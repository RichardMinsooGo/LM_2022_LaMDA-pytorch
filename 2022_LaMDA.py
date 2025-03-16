

from __future__ import annotations
from typing import Optional, ClassVar
from dataclasses import dataclass, field

@dataclass
class CFG:

    """
    Configuration for ZeRO
    """

    use_zero: bool = field(
        default = False,
        metadata = {'help': 'whether to use zero'}
    )

    """
    Configuration for optimizer
    """

    lr: float = field(
        default = 0.0001,
        metadata = {'help': 'learning rate'}
    )

    """
    Configuration class for LaMDA model.
    """

    num_tokens: int = field(
        default = 50257,
        metadata = {'help': 'number of tokens'}
    )

    dim: int = field(
        default = 512,
        metadata = {'help': 'dimension of the embedding'}
    )

    depth: int = field(
        default = 6,
        metadata = {'help': 'depth of the transformer'}
    )

    heads: int = field(
        default = 4,
        metadata = {'help': 'number of heads in the transformer'}
    )

    dim_head: int = field(
        default = 64,
        metadata = {'help': 'dimension of the head'}
    )

    """
    Configuration for data loader.
    """

    use_huggingface: bool = field(
        default = True,
        metadata = {'help': 'Whether to use huggingface datasets'}
    )

    train_dataset_name: Optional[str] = field(
        default="the_pile", 
        metadata={"help": "Path to Hugging Face training dataset."}
    )

    eval_dataset_name: Optional[str] = field(
        default="the_pile", 
        metadata={"help": "Path to Hugging Face validation dataset."}
    )

    choose_train_split: Optional[str] = field(
        default="train", 
        metadata={"help": "Choose Hugging Face training dataset split."}
    )

    choose_eval_split: Optional[str] = field(
        default="train", 
        metadata={"help": "Choose Hugging Face validation dataset split."}
    )

    remove_train_columns: ClassVar[list[str]] = field(
        default = ['meta'], 
        metadata={"help": "Train dataset columns to remove."}
    )

    remove_eval_columns: ClassVar[list[str]] = field(
        default = ['meta'], 
        metadata={"help": "Validation dataset columns to remove."}
    )

    seed: Optional[int] = field(
        default=42, 
        metadata={"help": "Random seed used for reproducibility."}
    )

    tokenizer_name: Optional[str] = field(
        default="gpt2",
        metadata={"help": "Tokenizer name."}
    )

    tokenizer_seq_length: Optional[int] = field(
        default=512, 
        metadata={"help": "Sequence lengths used for tokenizing examples."}
    )

    select_input_string: Optional[str] = field(
        default="text", 
        metadata={"help": "Select the key to used as the input string column."}
    )
    
    batch_size: Optional[int] = field(
        default=16, 
        metadata={"help": "Batch size for training and validation."}
    )

    save_to_path: Optional[str] = field(
        default="''", 
        metadata={"help": "Save the dataset to local disk."}
    )

    """
    Configuration for Weights and Biases
    """

    use_wandb: bool = field(
        default = False,
        metadata = {'help': 'Whether to use Weights and Biases for logging'}
    )

    project_name: Optional[str] = field(
        default="LaMDA pre-training",
        metadata = {'help': 'Name of the project'}
    )

import torch
from torch import nn, einsum
import torch.nn.functional as F

import math

from einops import rearrange

# from lamda_pytorch.config.config import CFG

# residual wrapper

class Residual(nn.Module):
    def __init__(self, fn):
        super().__init__()
        self.fn = fn

    def forward(self, x, **kwargs):
        return self.fn(x, **kwargs) + x

# pre-normalization wrapper

class PreNorm(nn.Module):
    def __init__(self, dim, fn):
        super().__init__()
        self.norm = nn.LayerNorm(dim)
        self.fn = fn

    def forward(self, x, **kwargs):
        return self.fn(self.norm(x), **kwargs)

# gated-GELU activation function

class GEGLU(nn.Module):
    def forward(self, x):
        x, gates = x.chunk(2, dim = -1)
        return x * F.gelu(gates)

# feedforward layer with gated-GELU activation function

class FeedForward(nn.Module):
    def __init__(self, dim, mult = 4, dropout = 0.):
        super().__init__()
        inner_dim = int(dim * mult)
        self.net = nn.Sequential(
            nn.Linear(dim, inner_dim * 2),
            GEGLU(),
            nn.Dropout(dropout), # optional dropout
            nn.Linear(inner_dim, dim)
        )

    def forward(self, x):
        return self.net(x)

# T5 relative positional bias

class T5RelativePositionBias(nn.Module):
    def __init__(
        self,
        scale,
        num_buckets = 32,
        max_distance = 128,
        heads = 8
    ):
        super().__init__()
        self.scale = scale
        self.num_buckets = num_buckets
        self.max_distance = max_distance
        self.relative_attention_bias = nn.Embedding(num_buckets, heads)

    @staticmethod
    def _relative_position_bucket(
        relative_position,
        num_buckets = 32,
        max_distance = 128
    ):
        n = -relative_position
        n = torch.max(n, torch.zeros_like(n))

        max_exact = num_buckets // 2
        is_small = n < max_exact

        val_if_large = max_exact + (torch.log(n.float() / max_exact) / math.log(max_distance / max_exact) * (num_buckets - max_exact)).long()
        val_if_large = torch.min(val_if_large, torch.full_like(val_if_large, num_buckets - 1))
        return torch.where(is_small, n, val_if_large)

    def forward(self, qk_dots):
        i, j, device = *qk_dots.shape[-2:], qk_dots.device
        q_pos = torch.arange(i, dtype = torch.long, device = device)
        k_pos = torch.arange(j, dtype = torch.long, device = device)
        rel_pos = rearrange(k_pos, 'j -> 1 j') - rearrange(q_pos, 'i -> i 1')
        rp_bucket = self._relative_position_bucket(rel_pos, num_buckets = self.num_buckets, max_distance = self.max_distance)
        values = self.relative_attention_bias(rp_bucket)
        bias = rearrange(values, 'i j h -> () h i j')
        return qk_dots + (bias * self.scale)

# attention

class Attention(nn.Module):
    def __init__(
        self,
        *,
        dim,
        heads = 8,
        dim_head = 64,
        dropout = 0.
    ):
        super().__init__()
        self.heads = heads
        self.scale = dim_head ** -0.5
        inner_dim = heads * dim_head

        self.dropout = nn.Dropout(dropout)

        self.to_q = nn.Linear(dim, inner_dim, bias = False)
        self.to_kv = nn.Linear(dim, dim_head * 2, bias = False)
        self.to_out = nn.Linear(inner_dim, dim)

        self.rel_pos_bias = T5RelativePositionBias(scale = dim_head ** 0.5, heads = heads)

    def forward(self, x):
        h, device = self.heads, x.device

        q, k, v = (self.to_q(x), *self.to_kv(x).chunk(2, dim = -1))

        q = rearrange(q, 'b n (h d) -> b h n d', h = h)
        q = q * self.scale

        sim = einsum('b h i d, b j d -> b h i j', q, k)
        i, j = sim.shape[-2:]

        # T5 Relative Positional Bias
        sim = self.rel_pos_bias(sim)

        # Causal Mask
        causal_mask = torch.ones((i, j), dtype = torch.bool, device = device).triu(j - i + 1)
        sim = sim.masked_fill(causal_mask, -torch.finfo(sim.dtype).max)

        attn = sim.softmax(dim = -1)

        attn = self.dropout(attn) # Optional dropout

        out = einsum('b h i j, b j d -> b h i d', attn, v)

        out = rearrange(out, 'b h n d -> b n (h d)')
        return self.to_out(out)

# Transformer

class Transformer(nn.Module):
    def __init__(self, dim, depth, heads, dim_head, dropout = 0.):
        super().__init__()
        self.layers = nn.ModuleList([])
        for _ in range(depth):
            self.layers.append(nn.ModuleList([
                Residual(PreNorm(dim, Attention(dim = dim, heads = heads, dim_head = dim_head, dropout = dropout))),
                Residual(PreNorm(dim, FeedForward(dim = dim, dropout = dropout)))
            ]))
    def forward(self, x):
        for attn, ff in self.layers:
            x = attn(x)
            x = ff(x)
        return x

# LaMDA Model

class LaMDA(nn.Module):
    def __init__(self, *, num_tokens, dim, depth, dim_head, heads):
        super().__init__()
        self.token_emb = nn.Embedding(num_tokens, dim)

        self.transformer = Transformer(dim, depth, dim_head, heads)

        self.to_logits = nn.Sequential(
            nn.LayerNorm(dim),
            nn.Linear(dim, num_tokens)
        )

    def forward(self, x):
        x = self.token_emb(x)
        x = self.transformer(x)
        logits = self.to_logits(x)
        return logits

def lamda_model():
    model = LaMDA(
        num_tokens = CFG.num_tokens,
        dim        = CFG.dim,
        depth      = CFG.depth,
        dim_head   = CFG.dim_head,
        heads      = CFG.heads
    )
    return model

if __name__ == "__main__":

    lamda_base = lamda_model()

    #lamda = AutoregressiveWrapper(lamda_base, max_seq_len = 2048)

    tokens = torch.randint(0, 20000, (1, 2048)) # mock token data

    logits = lamda_base(tokens)
    print(logits.shape)

    n_params_torch = sum(
        p.numel() for p in lamda_base.parameters() if p.requires_grad
    )

    print(f"Number of parameters in torch model: {n_params_torch}")


    tokens = torch.randint(0, 20000, (16, 878)) # mock token data

    logits = lamda_base(tokens)
    print(logits.shape)







