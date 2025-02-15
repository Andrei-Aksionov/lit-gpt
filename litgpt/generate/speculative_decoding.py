# Copyright Lightning AI. Licensed under the Apache License 2.0, see LICENSE file.

import sys
import time
import warnings
from pathlib import Path
from pprint import pprint
from typing import Any, Dict, Iterator, List, Literal, Optional, Tuple, Union

import lightning as L
import torch
import torch._dynamo.config
import torch._inductor.config
import torch.nn.functional as F
from lightning.fabric.plugins import BitsandbytesPrecision
from lightning_utilities.core.imports import RequirementCache

from litgpt.config import Config
from litgpt.model import GPT
from litgpt.prompts import PromptStyle, has_prompt_style, load_prompt_style
from litgpt.tokenizer import Tokenizer
from litgpt.generate.base import multinomial_num_samples_1, sample_top_p, next_token
from litgpt.utils import (
    check_file_size_on_cpu_and_warn,
    check_valid_checkpoint_dir,
    extend_checkpoint_dir,
    get_default_supported_precision,
    load_checkpoint,
)

# TODO (andrei aksionau): Remove this
# flake8: noqa


def sample(
    logits: torch.Tensor,
    temperature: float = 1.0,
    top_k: Optional[int] = None,
    top_p: float = 1.0,
    apply_softmax: bool = True,
) -> torch.Tensor:
    if top_p < 0.0 or top_p > 1.0:
        raise ValueError(f"top_p must be in [0, 1], got {top_p}")
    logits = logits[0, -1]
    # optionally crop the logits to only the top k options
    if top_k is not None:
        v, i = torch.topk(logits, min(top_k, logits.size(-1)))
        # do not use `torch.where` as in nanogpt because it will repeat top-k collisions
        fill_value = float("-inf") if apply_softmax else float(0)
        logits = torch.full_like(logits, fill_value).scatter_(-1, i, v)
    # optionally scale the logits and sample from a probability distribution
    if temperature > 0.0 or top_p > 0.0:
        if temperature > 0.0:
            logits = logits / temperature
        # optionally crop the logits to smallest set of logits with a cumulative probability above top_p
        if top_p < 1.0:
            logits = sample_top_p(logits, top_p)
        probs = F.softmax(logits, dim=-1) if apply_softmax else logits
        return multinomial_num_samples_1(probs), probs
    return torch.argmax(logits, dim=-1, keepdim=True), F.softmax(logits, dim=-1)


def speculative_decoding(
    draft_model: GPT,
    target_model: GPT,
    token: torch.Tensor,
    input_pos: torch.Tensor,
    input_pos_maxp1: torch.Tensor,
    speculative_k: int,
    **sample_kwargs: Dict[str, Any],
):
    # Step 1: autoregressive generation of new tokens with the draft model
    draft_input_pos = input_pos.clone()
    draft_input_pos_maxp1 = input_pos_maxp1.clone()
    draft_tokens, draft_probs = [], []
    draft_token = token
    for idx in range(speculative_k):
        logits = draft_model(idx=draft_token.unsqueeze(0), input_pos=draft_input_pos, input_pos_maxp1=draft_input_pos_maxp1)
        draft_token, draft_prob = sample(logits, **sample_kwargs)
        draft_input_pos.add_(1)
        draft_input_pos_maxp1.add_(1)
        draft_tokens.append(draft_token)
        draft_probs.append(draft_prob)

    draft_tokens = torch.cat(draft_tokens)

    # Step 2: process draft tokens with the target model
    candidate_tokens = torch.cat((token, draft_tokens))
    candidate_input_pos = input_pos + torch.arange(0, speculative_k + 1, device=input_pos.device)
    candidate_input_pos_maxp1 = input_pos_maxp1.add(speculative_k)
    target_logits = target_model(idx=candidate_tokens.unsqueeze(0), input_pos=candidate_input_pos, input_pos_maxp1=candidate_input_pos_maxp1)

    # Step 3: convert target logits to probabilities
    target_probs = []
    for target_logit in target_logits.split(1, dim=1):
        _, target_prob = sample(target_logit, **sample_kwargs)
        target_probs.append(target_prob)

    # Step 4: Iterate over draft and target tokens, decide whether to accept a draft token or not
    accepted_tokens = []
    for idx in range(len(draft_tokens)):
        draft_token = draft_tokens[idx].unsqueeze(0)
        draft_prob = draft_probs[idx][draft_token]
        target_prob = target_probs[idx][draft_token]

        # if target prob for the draft token is equal or larger than draft prob - keep and continue
        if target_prob >= draft_prob:
            accepted_tokens.append(draft_token)
            continue

        # discard the draft token only with a probability
        discard_prob = 1 - target_prob / draft_prob
        should_discard_token = torch.rand(1, device=discard_prob.device) <= discard_prob

        if not should_discard_token:
            accepted_tokens.append(draft_token)
            continue

        # if discarded - update the distribution, sample a new token and break the loop
        adjusted_distribution = target_probs[idx] - draft_probs[idx]
        adjusted_distribution = torch.clamp(adjusted_distribution, 0.0)
        adjusted_distribution = adjusted_distribution / adjusted_distribution.sum()
        new_token, _ = sample(adjusted_distribution[None, None, ...], apply_softmax=False, **sample_kwargs)
        return torch.cat((*accepted_tokens, new_token))

    # if all the candidate tokens were accepted
    # calculate kv-cache for the last draft token
    draft_model(idx=draft_token.unsqueeze(0), input_pos=draft_input_pos, input_pos_maxp1=draft_input_pos_maxp1)
    # sample the last token
    new_token, _ = sample(target_logits, **sample_kwargs)
    return torch.cat((*accepted_tokens, new_token))


@torch.inference_mode()
def generate_fn(
    draft_model: GPT,
    target_model: GPT,
    prompt: torch.Tensor,
    max_returned_tokens: int,
    *,
    temperature: float = 1.0,
    top_k: Optional[int] = None,
    top_p: float = 1.0,
    stop_tokens: Tuple[List[int], ...] = (),
    include_prompt: bool,
    include_eos: bool,
    speculative_k: int,
) -> Iterator[torch.Tensor]:
    """
    Generates tokens for a single prompt.

    Args:
        model: The model to use.
        prompt: The tokenized prompt to generate from.
        max_returned_tokens: The maximum number of new tokens to return. Does not include the prompt tokens.
        temperature: The temp to pass to sample().
        top_k: The top_k to pass to sample().
        top_p: The top_p to pass to sample().
        stop_tokens: A tuple of stop sequences. If any of the sequences are generated, the generation stops early before max_returned_tokens.
        include_prompt: Whether to output the prompt tokens.
        include_eos: Whether to output the stop tokens if generation stops early.
    """

    prompt_size = prompt.size(0)
    device = prompt.device

    assert max_returned_tokens > prompt_size, f"Not enough space for {prompt_size} prompt tokens in a context length of {max_returned_tokens}."
    if draft_model.max_seq_length < max_returned_tokens - 1:
        raise NotImplementedError(f"max_seq_length {draft_model.max_seq_length} needs to be >= {max_returned_tokens - 1}")
    if target_model.max_seq_length < max_returned_tokens - 1:
        raise NotImplementedError(f"max_seq_length {target_model.max_seq_length} needs to be >= {max_returned_tokens - 1}")

    # Step 1: Prefill draft and target models with the prompt.
    input_pos = torch.arange(0, prompt_size, device=device, dtype=torch.int64)
    input_pos_maxp1 = torch.tensor(prompt_size, device=device)
    next_token(
        draft_model,
        input_pos,
        prompt.view(1, -1),
        input_pos_maxp1=input_pos_maxp1,
        temperature=temperature,
        top_k=top_k,
        top_p=top_p,
    )
    token = next_token(
        target_model,
        input_pos,
        prompt.view(1, -1),
        input_pos_maxp1=input_pos_maxp1,
        temperature=temperature,
        top_k=top_k,
        top_p=top_p,
    )
    input_pos = torch.tensor([prompt_size], device=device, dtype=torch.int64)
    input_pos_maxp1.add_(1)

    # Step 2: Generate tokens in a speculative manner.
    tokens = []
    total_generated, total_accepted = 0, 0
    while input_pos < (max_returned_tokens - prompt_size):
        _speculative_k = min(speculative_k, (max_returned_tokens - prompt_size - input_pos).item())
        new_tokens = speculative_decoding(
            draft_model=draft_model,
            target_model=target_model,
            token=token,
            input_pos=input_pos,
            input_pos_maxp1=input_pos_maxp1,
            speculative_k=_speculative_k,
            temperature=temperature,
            top_k=top_k,
            top_p=top_p,
        )
        # check how many tokens are generated
        accepted_tokens_len = len(new_tokens)

        total_generated += _speculative_k
        total_accepted += accepted_tokens_len - 1  # returns always +1 to what was accepted

        # update input_pos and input_pos_maxp1
        input_pos.add_(accepted_tokens_len)
        input_pos_maxp1.add_(accepted_tokens_len)

        token = new_tokens[-1].unsqueeze(0)
        tokens.extend(new_tokens)

    tokens = [t.unsqueeze(0) for t in tokens]
    if include_prompt:
        tokens = [t.to(torch.int64) for t in prompt.split(1)] + tokens
    acceptance_rate = total_accepted / total_generated
    return tokens, acceptance_rate


@torch.inference_mode()
def generate(
    draft_model: GPT,
    target_model: GPT,
    prompt: torch.Tensor,
    max_returned_tokens: int,
    *,
    temperature: float = 1.0,
    top_k: Optional[int] = None,
    top_p: float = 1.0,
    eos_id: Optional[int] = None,
    include_prompt: bool = True,
    speculative_k: int,
) -> torch.Tensor:
    """
    Takes a conditioning sequence (prompt) as input and continues to generate as many tokens as requested.
    The implementation of this function is modified from A. Karpathy's nanoGPT.

    Args:
        model: The model to use.
        prompt: Tensor of shape (T) with indices of the prompt sequence.
        max_returned_tokens: The maximum number of tokens to return (given plus generated).
        temperature: Scales the predicted logits by 1 / temperature.
        top_k: If specified, only sample among the tokens with the k highest probabilities.
        top_p: If specified, it represents the cumulative probability threshold to consider in the sampling process.
            In top-p sampling, the next token is sampled from the highest probability tokens
            whose cumulative probability exceeds the threshold `top_p`. When specified,
            it must be `0 <= top_p <= 1`. Here, `top_p=0` is equivalent
            to sampling the most probable token, while `top_p=1` samples from the whole distribution.
            It can be used in conjunction with `top_k` and `temperature` with the following order
            of application:

            1. `top_k` sampling
            2. `temperature` scaling
            3. `top_p` sampling

            For more details, see https://arxiv.org/abs/1904.09751
            or https://huyenchip.com/2024/01/16/sampling.html#top_p
        eos_id: If specified, stop generating any more token once the <eos> token is triggered.
        include_prompt: If true (default) prepends the prompt (after applying the prompt style) to the output.
    """

    token_list, acceptance_rate = generate_fn(
        include_prompt=include_prompt,
        include_eos=True,
        draft_model=draft_model,
        target_model=target_model,
        prompt=prompt,
        max_returned_tokens=max_returned_tokens,
        temperature=temperature,
        top_k=top_k,
        top_p=top_p,
        stop_tokens=(([eos_id],) if eos_id is not None else ()),
        speculative_k=speculative_k,
    )

    return (torch.cat(token_list), acceptance_rate) if len(token_list) != 0 else (torch.Tensor(), None)


@torch.inference_mode()
def main(
    draft_model_checkpoint_dir: Path,
    target_model_checkpoint_dir: Path,
    prompt: str = "What food do llamas eat?",
    *,
    num_samples: int = 1,
    max_new_tokens: int = 50,
    speculative_k: int = 3,
    top_k: Optional[int] = 50,
    top_p: float = 1.0,
    temperature: float = 0.8,
    quantize: Optional[Literal["bnb.nf4", "bnb.nf4-dq", "bnb.fp4", "bnb.fp4-dq", "bnb.int8"]] = None,
    precision: Optional[str] = None,
    compile: bool = False,
) -> None:
    """Default generation option.

    Generates text samples based on a pre-trained model and tokenizer.

    Args:
        checkpoint_dir: The checkpoint directory to load.
        prompt: The prompt string to use for generating the samples.
        num_samples: The number of text samples to generate.
        max_new_tokens: The number of generation steps to take.
        top_k: The number of top most probable tokens to consider in the sampling process.
        top_p: If specified, it represents the cumulative probability threshold to consider in the sampling process.
            In top-p sampling, the next token is sampled from the highest probability tokens
            whose cumulative probability exceeds the threshold `top_p`. When specified,
            it must be `0 <= top_p <= 1`. Here, `top_p=0` is equivalent
            to sampling the most probable token, while `top_p=1` samples from the whole distribution.
            It can be used in conjunction with `top_k` and `temperature` with the following order
            of application:

            1. `top_k` sampling
            2. `temperature` scaling
            3. `top_p` sampling

            For more details, see https://arxiv.org/abs/1904.09751
            or https://huyenchip.com/2024/01/16/sampling.html#top_p
        temperature: A value controlling the randomness of the sampling process. Higher values result in more random
            samples.
        quantize: Whether to quantize the model and using which method:
            - bnb.nf4, bnb.nf4-dq, bnb.fp4, bnb.fp4-dq: 4-bit quantization from bitsandbytes
            - bnb.int8: 8-bit quantization from bitsandbytes
            for more details, see https://github.com/Lightning-AI/litgpt/blob/main/tutorials/quantize.md
        precision: Indicates the Fabric precision setting to use.
        compile: Whether to compile the model.
    """
    draft_model_checkpoint_dir = extend_checkpoint_dir(draft_model_checkpoint_dir)
    target_model_checkpoint_dir = extend_checkpoint_dir(target_model_checkpoint_dir)
    pprint(locals())

    precision = precision or get_default_supported_precision(training=False)

    plugins = None
    if quantize is not None and quantize.startswith("bnb."):
        if "mixed" in precision:
            raise ValueError("Quantization and mixed precision is not supported.")
        if RequirementCache("bitsandbytes != 0.42.0"):
            warnings.warn("LitGPT only supports bitsandbytes v0.42.0. This may result in errors when using quantization.")
        dtype = {"16-true": torch.float16, "bf16-true": torch.bfloat16, "32-true": torch.float32}[precision]
        plugins = BitsandbytesPrecision(quantize[4:], dtype)
        precision = None

    fabric = L.Fabric(devices=1, precision=precision, plugins=plugins)

    check_valid_checkpoint_dir(draft_model_checkpoint_dir)
    check_valid_checkpoint_dir(target_model_checkpoint_dir)
    draft_model_config = Config.from_file(draft_model_checkpoint_dir / "model_config.yaml")
    target_model_config = Config.from_file(target_model_checkpoint_dir / "model_config.yaml")

    draft_model_checkpoint_path = draft_model_checkpoint_dir / "lit_model.pth"
    target_model_checkpoint_path = target_model_checkpoint_dir / "lit_model.pth"
    check_file_size_on_cpu_and_warn(draft_model_checkpoint_path, fabric.device)
    check_file_size_on_cpu_and_warn(target_model_checkpoint_path, fabric.device)

    draft_tokenizer = Tokenizer(draft_model_checkpoint_dir)
    target_tokenizer = Tokenizer(target_model_checkpoint_dir)
    # TODO (andrei aksionau): add check that the tokenizer is the same, not just vocab size
    if draft_tokenizer.vocab_size != target_tokenizer.vocab_size:
        raise ValueError("Draft and target models have different vocab sizes.")

    tokenizer = Tokenizer(target_model_checkpoint_dir)
    prompt_style = (
        load_prompt_style(target_model_checkpoint_dir) if has_prompt_style(target_model_checkpoint_dir) else PromptStyle.from_config(target_model_config)
    )

    prompt = prompt_style.apply(prompt)
    encoded = tokenizer.encode(prompt, device=fabric.device)
    prompt_length = encoded.size(0)
    max_returned_tokens = prompt_length + max_new_tokens

    fabric.print(f"Loading draft model {str(draft_model_checkpoint_path)!r} with {draft_model_config.__dict__}", file=sys.stderr)
    fabric.print(
        f"Loading target model {str(target_model_checkpoint_path)!r} with {target_model_config.__dict__}",
        file=sys.stderr,
    )
    t0 = time.perf_counter()
    with fabric.init_module(empty_init=True):
        draft_model = GPT(draft_model_config)
        target_model = GPT(target_model_config)
    fabric.print(f"Time to instantiate models: {time.perf_counter() - t0:.02f} seconds.", file=sys.stderr)
    with fabric.init_tensor():
        # set the max_seq_length to limit the memory usage to what we need
        draft_model.max_seq_length = max_returned_tokens
        target_model.max_seq_length = max_returned_tokens
        # enable the kv cache
        draft_model.set_kv_cache(batch_size=1)
        target_model.set_kv_cache(batch_size=1)
    draft_model.eval()
    target_model.eval()

    if compile:
        torch._dynamo.config.automatic_dynamic_shapes = True
        torch._inductor.config.triton.unique_kernel_names = True
        torch._inductor.config.coordinate_descent_tuning = True
        global next_token
        next_token = torch.compile(next_token, mode="reduce-overhead")

    draft_model = fabric.setup_module(draft_model)
    target_model = fabric.setup_module(target_model)

    t0 = time.perf_counter()
    load_checkpoint(fabric, draft_model, draft_model_checkpoint_path)
    load_checkpoint(fabric, target_model, target_model_checkpoint_path)
    fabric.print(f"Time to load the models weights: {time.perf_counter() - t0:.02f} seconds.", file=sys.stderr)

    L.seed_everything(1234)
    for i in range(num_samples):
        t0 = time.perf_counter()
        y, acceptance_rate = generate(
            draft_model,
            target_model,
            encoded,
            max_returned_tokens,
            temperature=temperature,
            top_k=top_k,
            top_p=top_p,
            eos_id=tokenizer.eos_id,
            speculative_k=speculative_k,
        )
        t = time.perf_counter() - t0
        for block in draft_model.transformer.h:
            block.attn.kv_cache.reset_parameters()
        for block in target_model.transformer.h:
            block.attn.kv_cache.reset_parameters()
        fabric.print(tokenizer.decode(y))
        tokens_generated = y.size(0) - prompt_length
        print(f"Acceptance rate: {acceptance_rate * 100:.2f}%")
        fabric.print(f"Time for inference {i + 1}: {t:.02f} sec total, {tokens_generated / t:.02f} tokens/sec", file=sys.stderr)
    if fabric.device.type == "cuda":
        fabric.print(f"Memory used: {torch.cuda.max_memory_allocated() / 1e9:.02f} GB", file=sys.stderr)


if __name__ == "__main__":
    draft_model_checkpoint_dir = Path("checkpoints/EleutherAI/pythia-14m")
    target_model_checkpoint_dir = Path("checkpoints/EleutherAI/pythia-14m")

    # draft_model_checkpoint_dir = Path("checkpoints/EleutherAI/pythia-160m")
    # target_model_checkpoint_dir = Path("checkpoints/EleutherAI/pythia-160m")

    # draft_model_checkpoint_dir = Path("checkpoints/EleutherAI/pythia-410m")
    # target_model_checkpoint_dir = Path("checkpoints/EleutherAI/pythia-410m")

    main(draft_model_checkpoint_dir, target_model_checkpoint_dir, max_new_tokens=50)
