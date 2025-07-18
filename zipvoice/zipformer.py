#!/usr/bin/env python3
# Copyright    2022-2024  Xiaomi Corp.        (authors: Daniel Povey,
#                                                       Zengwei Yao,
#                                                       Wei Kang
#                                                       Han Zhu)
#
# See ../../../../LICENSE for clarification regarding multiple authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import copy
import logging
import math
import random
from typing import Optional, Tuple, Union

import torch
from scaling import (
    ActivationDropoutAndLinear,
    Balancer,
    BiasNorm,
    Dropout2,
    FloatLike,
    Identity,
    ScaledLinear,
    ScheduledFloat,
    SwooshR,
    Whiten,
    limit_param_value,
    penalize_abs_values_gt,
    softmax,
)
from torch import Tensor, nn


def timestep_embedding(timesteps, dim, max_period=10000):
    """Create sinusoidal timestep embeddings.

    :param timesteps: shape of (N) or (N, T)
    :param dim: the dimension of the output.
    :param max_period: controls the minimum frequency of the embeddings.
    :return: an Tensor of positional embeddings. shape of (N, dim) or (T, N, dim)
    """
    half = dim // 2
    freqs = torch.exp(
        -math.log(max_period)
        * torch.arange(start=0, end=half, dtype=torch.float32, device=timesteps.device)
        / half
    )

    if timesteps.dim() == 2:
        timesteps = timesteps.transpose(0, 1)  # (N, T) -> (T, N)

    args = timesteps[..., None].float() * freqs[None]
    embedding = torch.cat([torch.cos(args), torch.sin(args)], dim=-1)
    if dim % 2:
        embedding = torch.cat([embedding, torch.zeros_like(embedding[..., :1])], dim=-1)
    return embedding


class TTSZipformer(nn.Module):
    """
    Args:

    Note: all "int or Tuple[int]" arguments below will be treated as lists of the same
    length as downsampling_factor if they are single ints or one-element tuples.
    The length of downsampling_factor defines the number of stacks.

        downsampling_factor (Tuple[int]): downsampling factor for each encoder stack.
           Note: this is in addition to the downsampling factor of 2 that is applied in
           the frontend (self.encoder_embed).
        encoder_dim (Tuple[int]): embedding dimension of each of the encoder stacks,
            one per encoder stack.
        num_encoder_layers (int or Tuple[int])): number of encoder layers for each stack
        query_head_dim (int or Tuple[int]): dimension of query and key per attention
           head: per stack, if a tuple..
        pos_head_dim (int or Tuple[int]): dimension of positional-encoding projection
            per attention head
        value_head_dim (int or Tuple[int]): dimension of value in each attention head
        num_heads: (int or Tuple[int]): number of heads in the self-attention mechanism.
              Must be at least 4.
        feedforward_dim (int or Tuple[int]): hidden dimension in feedforward modules
        cnn_module_kernel (int or Tuple[int])): Kernel size of convolution module

        pos_dim (int): the dimension of each positional-encoding vector prior to
            projection, e.g. 128.

        dropout (float): dropout rate
        warmup_batches (float): number of batches to warm up over; this controls
          dropout of encoder layers.
        use_time_embed: (bool): if True, do not take time embedding as additional input.
        time_embed_dim: (int): the dimension of the time embedding.
    """

    def __init__(
        self,
        in_dim: int,
        out_dim: int,
        downsampling_factor: Tuple[int] = (2, 4),
        num_encoder_layers: Union[int, Tuple[int]] = 4,
        cnn_module_kernel: Union[int, Tuple[int]] = 31,
        encoder_dim: int = 384,
        query_head_dim: int = 24,
        pos_head_dim: int = 4,
        value_head_dim: int = 12,
        num_heads: int = 8,
        feedforward_dim: int = 1536,
        pos_dim: int = 192,
        dropout: FloatLike = None,  # see code below for default
        warmup_batches: float = 4000.0,
        use_time_embed: bool = True,
        time_embed_dim: int = 192,
        use_guidance_scale_embed: bool = False,
        guidance_scale_embed_dim: int = 192,
        use_conv: bool = True,
    ) -> None:
        super(TTSZipformer, self).__init__()

        if dropout is None:
            dropout = ScheduledFloat((0.0, 0.3), (20000.0, 0.1))

        def _to_tuple(x):
            """Converts a single int or a 1-tuple of an int to a tuple with the same
            length as downsampling_factor"""
            if isinstance(x, int):
                x = (x,)
            if len(x) == 1:
                x = x * len(downsampling_factor)
            else:
                assert len(x) == len(downsampling_factor) and isinstance(x[0], int)
            return x

        def _assert_downsampling_factor(factors):
            """assert downsampling_factor follows u-net style"""
            assert factors[0] == 1 and factors[-1] == 1

            for i in range(1, len(factors) // 2 + 1):
                assert factors[i] == factors[i - 1] * 2

            for i in range(len(factors) // 2 + 1, len(factors)):
                assert factors[i] * 2 == factors[i - 1]

        _assert_downsampling_factor(downsampling_factor)
        self.downsampling_factor = downsampling_factor  # tuple
        num_encoder_layers = _to_tuple(num_encoder_layers)
        self.cnn_module_kernel = cnn_module_kernel = _to_tuple(cnn_module_kernel)
        self.encoder_dim = encoder_dim
        self.num_encoder_layers = num_encoder_layers
        self.query_head_dim = query_head_dim
        self.value_head_dim = value_head_dim
        self.num_heads = num_heads

        self.use_time_embed = use_time_embed
        self.use_guidance_scale_embed = use_guidance_scale_embed

        self.time_embed_dim = time_embed_dim
        if self.use_time_embed:
            assert time_embed_dim != -1
        else:
            time_embed_dim = -1
        self.guidance_scale_embed_dim = guidance_scale_embed_dim

        self.in_proj = nn.Linear(in_dim, encoder_dim)
        self.out_proj = nn.Linear(encoder_dim, out_dim)

        # each one will be Zipformer2Encoder or DownsampledZipformer2Encoder
        encoders = []

        num_encoders = len(downsampling_factor)
        for i in range(num_encoders):
            encoder_layer = Zipformer2EncoderLayer(
                embed_dim=encoder_dim,
                pos_dim=pos_dim,
                num_heads=num_heads,
                query_head_dim=query_head_dim,
                pos_head_dim=pos_head_dim,
                value_head_dim=value_head_dim,
                feedforward_dim=feedforward_dim,
                use_conv=use_conv,
                cnn_module_kernel=cnn_module_kernel[i],
                dropout=dropout,
            )

            # For the segment of the warmup period, we let the Conv2dSubsampling
            # layer learn something.  Then we start to warm up the other encoders.
            encoder = Zipformer2Encoder(
                encoder_layer,
                num_encoder_layers[i],
                embed_dim=encoder_dim,
                time_embed_dim=time_embed_dim,
                pos_dim=pos_dim,
                warmup_begin=warmup_batches * (i + 1) / (num_encoders + 1),
                warmup_end=warmup_batches * (i + 2) / (num_encoders + 1),
                final_layerdrop_rate=0.035 * (downsampling_factor[i] ** 0.5),
            )

            if downsampling_factor[i] != 1:
                encoder = DownsampledZipformer2Encoder(
                    encoder,
                    dim=encoder_dim,
                    downsample=downsampling_factor[i],
                )

            encoders.append(encoder)

        self.encoders = nn.ModuleList(encoders)
        if self.use_time_embed:
            self.time_embed = nn.Sequential(
                nn.Linear(time_embed_dim, time_embed_dim * 2),
                SwooshR(),
                nn.Linear(time_embed_dim * 2, time_embed_dim),
            )
        else:
            self.time_embed = None

        if self.use_guidance_scale_embed:
            self.guidance_scale_embed = ScaledLinear(
                guidance_scale_embed_dim,
                time_embed_dim,
                bias=False,
                initial_scale=0.1,
            )
        else:
            self.guidance_scale_embed = None

    def forward(
        self,
        x: Tensor,
        t: Optional[Tensor] = None,
        padding_mask: Optional[Tensor] = None,
        guidance_scale: Optional[Tensor] = None,
    ) -> Tuple[Tensor, Tensor]:
        """
        Args:
          x:
            The input tensor. Its shape is (batch_size, seq_len, feature_dim).
          t:
            A t tensor of shape (batch_size,) or (batch_size, seq_len)
          padding_mask:
            The mask for padding, of shape (batch_size, seq_len); True means
            masked position. May be None.
        Returns:
          Return the output embeddings. its shape is
            (batch_size, output_seq_len, encoder_dim)
        """
        x = x.permute(1, 0, 2)
        x = self.in_proj(x)

        if t is not None:
            assert t.dim() == 1 or t.dim() == 2, t.shape
            time_emb = timestep_embedding(t, self.time_embed_dim)
            if guidance_scale is not None:
                assert (
                    guidance_scale.dim() == 1 or guidance_scale.dim() == 2
                ), guidance_scale.shape
                guidance_scale_emb = self.guidance_scale_embed(
                    timestep_embedding(guidance_scale, self.guidance_scale_embed_dim)
                )
                time_emb = time_emb + guidance_scale_emb
            time_emb = self.time_embed(time_emb)
        else:
            time_emb = None

        attn_mask = None

        for i, module in enumerate(self.encoders):
            x = module(
                x,
                time_emb=time_emb,
                src_key_padding_mask=padding_mask,
                attn_mask=attn_mask,
            )
        x = self.out_proj(x)
        x = x.permute(1, 0, 2)
        return x


def _whitening_schedule(x: float, ratio: float = 2.0) -> ScheduledFloat:
    return ScheduledFloat((0.0, x), (20000.0, ratio * x), default=x)


class Zipformer2EncoderLayer(nn.Module):
    """
    Args:
        embed_dim: the number of expected features in the input (required).
        nhead: the number of heads in the multiheadattention models (required).
        feedforward_dim: the dimension of the feedforward network model (required).
        dropout: the dropout value (default=0.1).
        cnn_module_kernel (int): Kernel size of convolution module (default=31).

    Examples::
        >>> encoder_layer = Zipformer2EncoderLayer(embed_dim=512, nhead=8)
        >>> src = torch.rand(10, 32, 512)
        >>> pos_emb = torch.rand(32, 19, 512)
        >>> out = encoder_layer(src, pos_emb)
    """

    def __init__(
        self,
        embed_dim: int,
        pos_dim: int,
        num_heads: int,
        query_head_dim: int,
        pos_head_dim: int,
        value_head_dim: int,
        feedforward_dim: int,
        dropout: FloatLike = 0.1,
        cnn_module_kernel: int = 31,
        use_conv: bool = True,
        attention_skip_rate: FloatLike = ScheduledFloat(
            (0.0, 0.2), (4000.0, 0.05), (16000, 0.0), default=0
        ),
        conv_skip_rate: FloatLike = ScheduledFloat(
            (0.0, 0.2), (4000.0, 0.05), (16000, 0.0), default=0
        ),
        const_attention_rate: FloatLike = ScheduledFloat(
            (0.0, 0.25), (4000.0, 0.025), default=0
        ),
        ff2_skip_rate: FloatLike = ScheduledFloat(
            (0.0, 0.1), (4000.0, 0.01), (50000.0, 0.0)
        ),
        ff3_skip_rate: FloatLike = ScheduledFloat(
            (0.0, 0.1), (4000.0, 0.01), (50000.0, 0.0)
        ),
        bypass_skip_rate: FloatLike = ScheduledFloat(
            (0.0, 0.5), (4000.0, 0.02), default=0
        ),
    ) -> None:
        super(Zipformer2EncoderLayer, self).__init__()
        self.embed_dim = embed_dim

        # self.bypass implements layer skipping as well as bypass.
        self.bypass = BypassModule(
            embed_dim, skip_rate=bypass_skip_rate, straight_through_rate=0
        )
        # bypass_mid is bypass used in the middle of the layer.
        self.bypass_mid = BypassModule(embed_dim, straight_through_rate=0)

        # skip probability for dynamic modules (meaning: anything but feedforward).
        self.attention_skip_rate = copy.deepcopy(attention_skip_rate)
        # an additional skip probability that applies to ConvModule to stop it from
        # contributing too much early on.
        self.conv_skip_rate = copy.deepcopy(conv_skip_rate)

        # ff2_skip_rate is to prevent the ff2 module from having output that's too big
        # compared to its residual.
        self.ff2_skip_rate = copy.deepcopy(ff2_skip_rate)
        self.ff3_skip_rate = copy.deepcopy(ff3_skip_rate)

        self.const_attention_rate = copy.deepcopy(const_attention_rate)

        self.self_attn_weights = RelPositionMultiheadAttentionWeights(
            embed_dim,
            pos_dim=pos_dim,
            num_heads=num_heads,
            query_head_dim=query_head_dim,
            pos_head_dim=pos_head_dim,
            dropout=0.0,
        )

        self.self_attn1 = SelfAttention(embed_dim, num_heads, value_head_dim)

        self.self_attn2 = SelfAttention(embed_dim, num_heads, value_head_dim)

        self.feed_forward1 = FeedforwardModule(
            embed_dim, (feedforward_dim * 3) // 4, dropout
        )

        self.feed_forward2 = FeedforwardModule(embed_dim, feedforward_dim, dropout)

        self.feed_forward3 = FeedforwardModule(
            embed_dim, (feedforward_dim * 5) // 4, dropout
        )

        self.nonlin_attention = NonlinAttention(
            embed_dim, hidden_channels=3 * embed_dim // 4
        )

        self.use_conv = use_conv

        if self.use_conv:
            self.conv_module1 = ConvolutionModule(embed_dim, cnn_module_kernel)

            self.conv_module2 = ConvolutionModule(embed_dim, cnn_module_kernel)

        self.norm = BiasNorm(embed_dim)

        self.balancer1 = Balancer(
            embed_dim,
            channel_dim=-1,
            min_positive=0.45,
            max_positive=0.55,
            min_abs=0.2,
            max_abs=4.0,
        )

        # balancer for output of NonlinAttentionModule
        self.balancer_na = Balancer(
            embed_dim,
            channel_dim=-1,
            min_positive=0.3,
            max_positive=0.7,
            min_abs=ScheduledFloat((0.0, 0.004), (4000.0, 0.02)),
            prob=0.05,  # out of concern for memory usage
        )

        # balancer for output of feedforward2, prevent it from staying too
        # small.  give this a very small probability, even at the start of
        # training, it's to fix a rare problem and it's OK to fix it slowly.
        self.balancer_ff2 = Balancer(
            embed_dim,
            channel_dim=-1,
            min_positive=0.3,
            max_positive=0.7,
            min_abs=ScheduledFloat((0.0, 0.0), (4000.0, 0.1), default=0.0),
            max_abs=2.0,
            prob=0.05,
        )

        self.balancer_ff3 = Balancer(
            embed_dim,
            channel_dim=-1,
            min_positive=0.3,
            max_positive=0.7,
            min_abs=ScheduledFloat((0.0, 0.0), (4000.0, 0.2), default=0.0),
            max_abs=4.0,
            prob=0.05,
        )

        self.whiten = Whiten(
            num_groups=1,
            whitening_limit=_whitening_schedule(4.0, ratio=3.0),
            prob=(0.025, 0.25),
            grad_scale=0.01,
        )

        self.balancer2 = Balancer(
            embed_dim,
            channel_dim=-1,
            min_positive=0.45,
            max_positive=0.55,
            min_abs=0.1,
            max_abs=4.0,
        )

    def get_sequence_dropout_mask(
        self, x: Tensor, dropout_rate: float
    ) -> Optional[Tensor]:
        if (
            dropout_rate == 0.0
            or not self.training
            or torch.jit.is_scripting()
            or torch.jit.is_tracing()
        ):
            return None
        batch_size = x.shape[1]
        mask = (torch.rand(batch_size, 1, device=x.device) > dropout_rate).to(x.dtype)
        return mask

    def sequence_dropout(self, x: Tensor, dropout_rate: float) -> Tensor:
        """
        Apply sequence-level dropout to x.
        x shape: (seq_len, batch_size, embed_dim)
        """
        dropout_mask = self.get_sequence_dropout_mask(x, dropout_rate)
        if dropout_mask is None:
            return x
        else:
            return x * dropout_mask

    def forward(
        self,
        src: Tensor,
        pos_emb: Tensor,
        time_emb: Optional[Tensor] = None,
        attn_mask: Optional[Tensor] = None,
        src_key_padding_mask: Optional[Tensor] = None,
    ) -> Tensor:
        """
        Pass the input through the encoder layer.
        Args:
          src: the sequence to the encoder (required):
            shape (seq_len, batch_size, embedding_dim).
          pos_emb: (1, 2*seq_len-1, pos_emb_dim) or
            (batch_size, 2*seq_len-1, pos_emb_dim)
          time_emb: the embedding representing the current timestep
            shape (batch_size, embedding_dim) or (seq_len, batch_size, embedding_dim).
          attn_mask: the attention mask, of shape (batch_size, seq_len, seq_len)
            or (seq_len, seq_len), interpreted as (batch_size, tgt_seq_len, src_seq_len)
            or (tgt_seq_len, src_seq_len). True means masked position. May be None.
          src_key_padding_mask:  the mask for padding, of shape (batch_size, seq_len);
            True means masked position.  May be None.

        Returns:
           A tensor which has the same shape as src
        """
        src_orig = src

        # dropout rate for non-feedforward submodules
        if torch.jit.is_scripting() or torch.jit.is_tracing():
            attention_skip_rate = 0.0
        else:
            attention_skip_rate = (
                float(self.attention_skip_rate) if self.training else 0.0
            )

        # attn_weights: (num_heads, batch_size, seq_len, seq_len)
        attn_weights = self.self_attn_weights(
            src,
            pos_emb=pos_emb,
            attn_mask=attn_mask,
            key_padding_mask=src_key_padding_mask,
        )
        if time_emb is not None:

            src = src + time_emb

        src = src + self.feed_forward1(src)

        self_attn_dropout_mask = self.get_sequence_dropout_mask(
            src, attention_skip_rate
        )

        selected_attn_weights = attn_weights[0:1]
        if torch.jit.is_scripting() or torch.jit.is_tracing():
            pass
        elif self.training and random.random() < float(self.const_attention_rate):
            # Make attention weights constant.  The intention is to
            # encourage these modules to do something similar to an
            # averaging-over-time operation.
            # only need the mask, can just use the 1st one and expand later
            selected_attn_weights = selected_attn_weights[0:1]
            selected_attn_weights = (selected_attn_weights > 0.0).to(
                selected_attn_weights.dtype
            )
            selected_attn_weights = selected_attn_weights * (
                1.0 / selected_attn_weights.sum(dim=-1, keepdim=True)
            )

        na = self.balancer_na(self.nonlin_attention(src, selected_attn_weights))

        src = src + (
            na if self_attn_dropout_mask is None else na * self_attn_dropout_mask
        )

        self_attn = self.self_attn1(src, attn_weights)

        src = src + (
            self_attn
            if self_attn_dropout_mask is None
            else self_attn * self_attn_dropout_mask
        )

        if self.use_conv:
            if torch.jit.is_scripting() or torch.jit.is_tracing():
                conv_skip_rate = 0.0
            else:
                conv_skip_rate = float(self.conv_skip_rate) if self.training else 0.0

            if time_emb is not None:
                src = src + time_emb

            src = src + self.sequence_dropout(
                self.conv_module1(
                    src,
                    src_key_padding_mask=src_key_padding_mask,
                ),
                conv_skip_rate,
            )

        if torch.jit.is_scripting() or torch.jit.is_tracing():
            ff2_skip_rate = 0.0
        else:
            ff2_skip_rate = float(self.ff2_skip_rate) if self.training else 0.0
        src = src + self.sequence_dropout(
            self.balancer_ff2(self.feed_forward2(src)), ff2_skip_rate
        )

        # bypass in the middle of the layer.
        src = self.bypass_mid(src_orig, src)

        self_attn = self.self_attn2(src, attn_weights)

        src = src + (
            self_attn
            if self_attn_dropout_mask is None
            else self_attn * self_attn_dropout_mask
        )

        if self.use_conv:

            if torch.jit.is_scripting() or torch.jit.is_tracing():
                conv_skip_rate = 0.0
            else:
                conv_skip_rate = float(self.conv_skip_rate) if self.training else 0.0

            if time_emb is not None:
                src = src + time_emb

            src = src + self.sequence_dropout(
                self.conv_module2(
                    src,
                    src_key_padding_mask=src_key_padding_mask,
                ),
                conv_skip_rate,
            )

        if torch.jit.is_scripting() or torch.jit.is_tracing():
            ff3_skip_rate = 0.0
        else:
            ff3_skip_rate = float(self.ff3_skip_rate) if self.training else 0.0
        src = src + self.sequence_dropout(
            self.balancer_ff3(self.feed_forward3(src)), ff3_skip_rate
        )

        src = self.balancer1(src)
        src = self.norm(src)

        src = self.bypass(src_orig, src)

        src = self.balancer2(src)
        src = self.whiten(src)

        return src


class Zipformer2Encoder(nn.Module):
    r"""Zipformer2Encoder is a stack of N encoder layers

    Args:
        encoder_layer: an instance of the Zipformer2EncoderLayer() class (required).
        num_layers: the number of sub-encoder-layers in the encoder (required).
        pos_dim: the dimension for the relative positional encoding

    Examples::
        >>> encoder_layer = Zipformer2EncoderLayer(embed_dim=512, nhead=8)
        >>> zipformer_encoder = Zipformer2Encoder(encoder_layer, num_layers=6)
        >>> src = torch.rand(10, 32, 512)
        >>> out = zipformer_encoder(src)
    """

    def __init__(
        self,
        encoder_layer: nn.Module,
        num_layers: int,
        embed_dim: int,
        time_embed_dim: int,
        pos_dim: int,
        warmup_begin: float,
        warmup_end: float,
        initial_layerdrop_rate: float = 0.5,
        final_layerdrop_rate: float = 0.05,
    ) -> None:
        super().__init__()
        self.encoder_pos = CompactRelPositionalEncoding(
            pos_dim, dropout_rate=0.15, length_factor=1.0
        )
        if time_embed_dim != -1:
            self.time_emb = nn.Sequential(
                SwooshR(),
                nn.Linear(time_embed_dim, embed_dim),
            )
        else:
            self.time_emb = None

        self.layers = nn.ModuleList(
            [copy.deepcopy(encoder_layer) for i in range(num_layers)]
        )
        self.num_layers = num_layers

        assert 0 <= warmup_begin <= warmup_end

        delta = (1.0 / num_layers) * (warmup_end - warmup_begin)
        cur_begin = warmup_begin  # interpreted as a training batch index
        for i in range(num_layers):
            cur_end = cur_begin + delta
            self.layers[i].bypass.skip_rate = ScheduledFloat(
                (cur_begin, initial_layerdrop_rate),
                (cur_end, final_layerdrop_rate),
                default=0.0,
            )
            cur_begin = cur_end

    def forward(
        self,
        src: Tensor,
        time_emb: Optional[Tensor] = None,
        attn_mask: Optional[Tensor] = None,
        src_key_padding_mask: Optional[Tensor] = None,
    ) -> Tensor:
        r"""Pass the input through the encoder layers in turn.

        Args:
            src: the sequence to the encoder (required):
                shape (seq_len, batch_size, embedding_dim).
            time_emb: the embedding representing the current timestep:
                shape  (batch_size, embedding_dim)
                or (seq_len, batch_size, embedding_dim) .
            attn_mask: the attention mask, of shape (batch_size, seq_len, seq_len)
                or (seq_len, seq_len), interpreted as
                (batch_size, tgt_seq_len, src_seq_len) or (tgt_seq_len, src_seq_len).
                True means masked position. May be None.
            src_key_padding_mask:  the mask for padding, of shape (batch_size, seq_len);
                True means masked position.  May be None.

        Returns: a Tensor with the same shape as src.
        """
        pos_emb = self.encoder_pos(src)
        if self.time_emb is not None:
            assert time_emb is not None
            time_emb = self.time_emb(time_emb)
        else:
            assert time_emb is None

        output = src

        for i, mod in enumerate(self.layers):
            output = mod(
                output,
                pos_emb,
                time_emb=time_emb,
                attn_mask=attn_mask,
                src_key_padding_mask=src_key_padding_mask,
            )

        return output


class BypassModule(nn.Module):
    """
    An nn.Module that implements a learnable bypass scale, and also randomized
    per-sequence layer-skipping.  The bypass is limited during early stages of training
    to be close to "straight-through", i.e. to not do the bypass operation much
    initially, in order to force all the modules to learn something.
    """

    def __init__(
        self,
        embed_dim: int,
        skip_rate: FloatLike = 0.0,
        straight_through_rate: FloatLike = 0.0,
        scale_min: FloatLike = ScheduledFloat((0.0, 0.9), (20000.0, 0.2), default=0),
        scale_max: FloatLike = 1.0,
    ):
        super().__init__()
        self.bypass_scale = nn.Parameter(torch.full((embed_dim,), 0.5))
        self.skip_rate = copy.deepcopy(skip_rate)
        self.straight_through_rate = copy.deepcopy(straight_through_rate)
        self.scale_min = copy.deepcopy(scale_min)
        self.scale_max = copy.deepcopy(scale_max)

    def _get_bypass_scale(self, batch_size: int):
        # returns bypass-scale of shape (num_channels,),
        # or (batch_size, num_channels,).  This is actually the
        # scale on the non-residual term, so 0 corresponds to bypassing
        # this module.
        if torch.jit.is_scripting() or torch.jit.is_tracing() or not self.training:
            return self.bypass_scale
        else:
            ans = limit_param_value(
                self.bypass_scale,
                min=float(self.scale_min),
                max=float(self.scale_max),
            )
            skip_rate = float(self.skip_rate)
            if skip_rate != 0.0:
                mask = torch.rand((batch_size, 1), device=ans.device) > skip_rate
                ans = ans * mask
                # now ans is of shape (batch_size, num_channels), and is zero for
                # sequences on which we have randomly chosen to do layer-skipping.
            straight_through_rate = float(self.straight_through_rate)
            if straight_through_rate != 0.0:
                mask = (
                    torch.rand((batch_size, 1), device=ans.device)
                    < straight_through_rate
                )
                ans = torch.maximum(ans, mask.to(ans.dtype))
            return ans

    def forward(self, src_orig: Tensor, src: Tensor):
        """
        Args: src_orig and src are both of shape (seq_len, batch_size, num_channels)
        Returns: something with the same shape as src and src_orig
        """
        bypass_scale = self._get_bypass_scale(src.shape[1])
        return src_orig + (src - src_orig) * bypass_scale


class DownsampledZipformer2Encoder(nn.Module):
    r"""
    DownsampledZipformer2Encoder is a zipformer encoder evaluated at a reduced frame
    rate, after convolutional downsampling, and then upsampled again at the output, and
    combined with the origin input, so that the output has the same shape as the input.
    """

    def __init__(self, encoder: nn.Module, dim: int, downsample: int):
        super(DownsampledZipformer2Encoder, self).__init__()
        self.downsample_factor = downsample
        self.downsample = SimpleDownsample(downsample)
        self.num_layers = encoder.num_layers
        self.encoder = encoder
        self.upsample = SimpleUpsample(downsample)
        self.out_combiner = BypassModule(dim, straight_through_rate=0)

    def forward(
        self,
        src: Tensor,
        time_emb: Optional[Tensor] = None,
        attn_mask: Optional[Tensor] = None,
        src_key_padding_mask: Optional[Tensor] = None,
    ) -> Tensor:
        r"""Downsample, go through encoder, upsample.

        Args:
            src: the sequence to the encoder (required):
                shape (seq_len, batch_size, embedding_dim).
            time_emb: the embedding representing the current timestep:
                shape  (batch_size, embedding_dim)
                or (seq_len, batch_size, embedding_dim) .
            feature_mask: something that broadcasts with src, that we'll multiply `src`
                by at every layer: if a Tensor, likely of shape
                (seq_len, batch_size, embedding_dim)
            attn_mask: the attention mask, of shape (batch_size, seq_len, seq_len)
                or (seq_len, seq_len), interpreted as
                (batch_size, tgt_seq_len, src_seq_len) or (tgt_seq_len, src_seq_len).
                True means masked position. May be None.
            src_key_padding_mask:  the mask for padding, of shape (batch_size, seq_len);
                True means masked position.  May be None.

        Returns: a Tensor with the same shape as src.
        """
        src_orig = src
        src = self.downsample(src)
        ds = self.downsample_factor
        if time_emb is not None and time_emb.dim() == 3:
            time_emb = time_emb[::ds]
        if attn_mask is not None:
            attn_mask = attn_mask[::ds, ::ds]
        if src_key_padding_mask is not None:
            src_key_padding_mask = src_key_padding_mask[..., ::ds]

        src = self.encoder(
            src,
            time_emb=time_emb,
            attn_mask=attn_mask,
            src_key_padding_mask=src_key_padding_mask,
        )
        src = self.upsample(src)
        # remove any extra frames that are not a multiple of downsample_factor
        src = src[: src_orig.shape[0]]

        return self.out_combiner(src_orig, src)


class SimpleDownsample(torch.nn.Module):
    """
    Does downsampling with attention, by weighted sum.
    """

    def __init__(self, downsample: int):
        super(SimpleDownsample, self).__init__()

        self.bias = nn.Parameter(torch.zeros(downsample))

        self.name = None  # will be set from training code

        self.downsample = downsample

    def forward(self, src: Tensor) -> Tensor:
        """
        x: (seq_len, batch_size, in_channels)
        Returns a tensor of shape
           ( (seq_len+downsample-1)//downsample, batch_size, channels)
        """
        (seq_len, batch_size, in_channels) = src.shape
        ds = self.downsample
        d_seq_len = (seq_len + ds - 1) // ds

        # Pad to an exact multiple of self.downsample
        # right-pad src, repeating the last element.
        pad = d_seq_len * ds - seq_len
        src_extra = src[src.shape[0] - 1 :].expand(pad, src.shape[1], src.shape[2])
        src = torch.cat((src, src_extra), dim=0)
        assert src.shape[0] == d_seq_len * ds

        src = src.reshape(d_seq_len, ds, batch_size, in_channels)

        weights = self.bias.softmax(dim=0)
        # weights: (downsample, 1, 1)
        weights = weights.unsqueeze(-1).unsqueeze(-1)

        # ans1 is the first `in_channels` channels of the output
        ans = (src * weights).sum(dim=1)

        return ans


class SimpleUpsample(torch.nn.Module):
    """
    A very simple form of upsampling that just repeats the input.
    """

    def __init__(self, upsample: int):
        super(SimpleUpsample, self).__init__()
        self.upsample = upsample

    def forward(self, src: Tensor) -> Tensor:
        """
        x: (seq_len, batch_size, num_channels)
        Returns a tensor of shape
           ( (seq_len*upsample), batch_size, num_channels)
        """
        upsample = self.upsample
        (seq_len, batch_size, num_channels) = src.shape
        src = src.unsqueeze(1).expand(seq_len, upsample, batch_size, num_channels)
        src = src.reshape(seq_len * upsample, batch_size, num_channels)
        return src


class CompactRelPositionalEncoding(torch.nn.Module):
    """
    Relative positional encoding module.  This version is "compact" meaning it is able
    to encode the important information about the relative position in a relatively
    small number of dimensions. The goal is to make it so that small differences between
    large relative offsets (e.g. 1000 vs. 1001) make very little difference to the
    embedding.   Such differences were potentially important when encoding absolute
    position, but not important when encoding relative position because there is now no
    need to compare two large offsets with each other.

    Our embedding works by projecting the interval [-infinity,infinity] to a finite
    interval using the atan() function, before doing the Fourier transform of that fixed
    interval.  The atan() function would compress the "long tails" too small, making it
    hard to distinguish between different magnitudes of large offsets, so we use a
    logarithmic function to compress large offsets to a smaller range before applying
    atan(). Scalings are chosen in such a way that the embedding can clearly distinguish
    individual offsets as long as they are quite close to the origin, e.g. abs(offset)
    <= about sqrt(embedding_dim)


    Args:
        embed_dim: Embedding dimension.
        dropout_rate: Dropout rate.
        max_len: Maximum input length: just a heuristic for initialization.
        length_factor: a heuristic scale (should be >= 1.0) which, if larger, gives
           less weight to small differences of offset near the origin.
    """

    def __init__(
        self,
        embed_dim: int,
        dropout_rate: FloatLike,
        max_len: int = 1000,
        length_factor: float = 1.0,
    ) -> None:
        """Construct a CompactRelPositionalEncoding object."""
        super(CompactRelPositionalEncoding, self).__init__()
        self.embed_dim = embed_dim
        assert embed_dim % 2 == 0, embed_dim
        self.dropout = Dropout2(dropout_rate)
        self.pe = None
        assert length_factor >= 1.0, length_factor
        self.length_factor = length_factor
        self.extend_pe(torch.tensor(0.0).expand(max_len))

    def extend_pe(self, x: Tensor, left_context_len: int = 0) -> None:
        """Reset the positional encodings."""
        T = x.size(0) + left_context_len

        if self.pe is not None:
            # self.pe contains both positive and negative parts
            # the length of self.pe is 2 * input_len - 1
            if self.pe.size(0) >= T * 2 - 1:
                self.pe = self.pe.to(dtype=x.dtype, device=x.device)
                return

        # if T == 4, x would contain [ -3, -2, 1, 0, 1, 2, 3 ]
        x = torch.arange(-(T - 1), T, device=x.device).to(torch.float32).unsqueeze(1)

        freqs = 1 + torch.arange(self.embed_dim // 2, device=x.device)

        # `compression_length` this is arbitrary/heuristic, if it is larger we have more
        # resolution for small time offsets but less resolution for large time offsets.
        compression_length = self.embed_dim**0.5
        # x_compressed, like X, goes from -infinity to infinity as T goes from -infinity
        # to infinity; but it does so more slowly than T for large absolute values of T.
        # The formula is chosen so that d(x_compressed )/dx is 1 around x == 0, which is
        # important.
        x_compressed = (
            compression_length
            * x.sign()
            * ((x.abs() + compression_length).log() - math.log(compression_length))
        )

        # if self.length_factor == 1.0, then length_scale is chosen so that the
        # FFT can exactly separate points close to the origin (T == 0).  So this
        # part of the formulation is not really heuristic.
        # But empirically, for ASR at least, length_factor > 1.0 seems to work better.
        length_scale = self.length_factor * self.embed_dim / (2.0 * math.pi)

        # note for machine implementations: if atan is not available, we can use:
        #   x.sign() * ((1 / (x.abs() + 1)) - 1)  * (-math.pi/2)
        #  check on wolframalpha.com: plot(sign(x) *  (1 / ( abs(x) + 1) - 1 ) * -pi/2 ,
        #  atan(x))
        x_atan = (x_compressed / length_scale).atan()  # results between -pi and pi

        cosines = (x_atan * freqs).cos()
        sines = (x_atan * freqs).sin()

        pe = torch.zeros(x.shape[0], self.embed_dim, device=x.device)
        pe[:, 0::2] = cosines
        pe[:, 1::2] = sines
        pe[:, -1] = 1.0  # for bias.

        self.pe = pe.to(dtype=x.dtype)

    def forward(self, x: Tensor, left_context_len: int = 0) -> Tensor:
        """Create positional encoding.

        Args:
            x (Tensor): Input tensor (time, batch, `*`).
            left_context_len: (int): Length of cached left context.

        Returns:
            positional embedding, of shape (batch, left_context_len + 2*time-1, `*`).
        """
        self.extend_pe(x, left_context_len)
        x_size_left = x.size(0) + left_context_len
        # length of positive side: x.size(0) + left_context_len
        # length of negative side: x.size(0)
        pos_emb = self.pe[
            self.pe.size(0) // 2
            - x_size_left
            + 1 : self.pe.size(0) // 2  # noqa E203
            + x.size(0),
            :,
        ]
        pos_emb = pos_emb.unsqueeze(0)
        return self.dropout(pos_emb)


class RelPositionMultiheadAttentionWeights(nn.Module):
    r"""Module that computes multi-head attention weights with relative position
    encoding. Various other modules consume the resulting attention weights:
    see, for example, the SimpleAttention module which allows you to compute
    conventional attention.

    This is a quite heavily modified from: "Transformer-XL: Attentive Language
        Models Beyond a Fixed-Length Context",
    we have to write up the differences.


    Args:
           embed_dim: number of channels at the input to this module, e.g. 256
             pos_dim: dimension of the positional encoding vectors, e.g. 128.
           num_heads:  number of heads to compute weights for, e.g. 8
     query_head_dim: dimension of the query (and key), per head.  e.g. 24.
       pos_head_dim: dimension of the projected positional encoding per head, e.g. 4.
            dropout: dropout probability for attn_output_weights. Default: 0.0.
       pos_emb_skip_rate: probability for skipping the pos_emb part of the scores on
                     any given call to forward(), in training time.
    """

    def __init__(
        self,
        embed_dim: int,
        pos_dim: int,
        num_heads: int,
        query_head_dim: int,
        pos_head_dim: int,
        dropout: float = 0.0,
        pos_emb_skip_rate: FloatLike = ScheduledFloat((0.0, 0.5), (4000.0, 0.0)),
    ) -> None:
        super().__init__()
        self.embed_dim = embed_dim
        self.num_heads = num_heads
        self.query_head_dim = query_head_dim
        self.pos_head_dim = pos_head_dim
        self.dropout = dropout
        self.pos_emb_skip_rate = copy.deepcopy(pos_emb_skip_rate)
        self.name = None  # will be overwritten in training code; for diagnostics.

        key_head_dim = query_head_dim
        in_proj_dim = (query_head_dim + key_head_dim + pos_head_dim) * num_heads

        # the initial_scale is supposed to take over the "scaling" factor of
        # head_dim ** -0.5 that has been used in previous forms of attention,
        # dividing it between the query and key.   Note: this module is intended
        # to be used with the ScaledAdam optimizer; with most other optimizers,
        # it would be necessary to apply the scaling factor in the forward function.
        self.in_proj = ScaledLinear(
            embed_dim,
            in_proj_dim,
            bias=True,
            initial_scale=query_head_dim**-0.25,
        )

        self.whiten_keys = Whiten(
            num_groups=num_heads,
            whitening_limit=_whitening_schedule(3.0),
            prob=(0.025, 0.25),
            grad_scale=0.025,
        )

        # add a balancer for the keys that runs with very small probability, and
        # tries to enforce that all dimensions have mean around zero.  The
        # weights produced by this module are invariant to adding a constant to
        # the keys, so the derivative of the bias is mathematically zero; but
        # due to how Adam/ScaledAdam work, it can learn a fairly large nonzero
        # bias because the small numerical roundoff tends to have a non-random
        # sign.  This module is intended to prevent that.  Use a very small
        # probability; that should be sufficient to fix the problem.
        self.balance_keys = Balancer(
            key_head_dim * num_heads,
            channel_dim=-1,
            min_positive=0.4,
            max_positive=0.6,
            min_abs=0.0,
            max_abs=100.0,
            prob=0.025,
        )

        # linear transformation for positional encoding.
        self.linear_pos = ScaledLinear(
            pos_dim, num_heads * pos_head_dim, bias=False, initial_scale=0.05
        )

        # the following are for diagnostics only, see --print-diagnostics option
        self.copy_pos_query = Identity()
        self.copy_query = Identity()

    def forward(
        self,
        x: Tensor,
        pos_emb: Tensor,
        key_padding_mask: Optional[Tensor] = None,
        attn_mask: Optional[Tensor] = None,
    ) -> Tensor:
        r"""
        Args:
            x: input of shape (seq_len, batch_size, embed_dim)
            pos_emb: Positional embedding tensor, of shape (1, 2*seq_len - 1, pos_dim)
            key_padding_mask: a bool tensor of shape (batch_size, seq_len).
                Positions that are True in this mask will be ignored as sources in the
                attention weighting.
            attn_mask: mask of shape (seq_len, seq_len) or
                (batch_size, seq_len, seq_len), interpreted as
                ([batch_size,] tgt_seq_len, src_seq_len)
               saying which positions are allowed to attend to which other positions.
        Returns:
           a tensor of attention weights, of
            shape (hum_heads, batch_size, seq_len, seq_len)
           interpreted as (hum_heads, batch_size, tgt_seq_len, src_seq_len).
        """
        x = self.in_proj(x)
        query_head_dim = self.query_head_dim
        pos_head_dim = self.pos_head_dim
        num_heads = self.num_heads

        seq_len, batch_size, _ = x.shape

        query_dim = query_head_dim * num_heads

        # self-attention
        q = x[..., 0:query_dim]
        k = x[..., query_dim : 2 * query_dim]
        # p is the position-encoding query
        p = x[..., 2 * query_dim :]
        assert p.shape[-1] == num_heads * pos_head_dim, (
            p.shape[-1],
            num_heads,
            pos_head_dim,
        )

        q = self.copy_query(q)  # for diagnostics only, does nothing.
        k = self.whiten_keys(self.balance_keys(k))  # does nothing in the forward pass.
        p = self.copy_pos_query(p)  # for diagnostics only, does nothing.

        q = q.reshape(seq_len, batch_size, num_heads, query_head_dim)
        p = p.reshape(seq_len, batch_size, num_heads, pos_head_dim)
        k = k.reshape(seq_len, batch_size, num_heads, query_head_dim)

        # time1 refers to target, time2 refers to source.
        q = q.permute(2, 1, 0, 3)  # (head, batch, time1, query_head_dim)
        p = p.permute(2, 1, 0, 3)  # (head, batch, time1, pos_head_dim)
        k = k.permute(2, 1, 3, 0)  # (head, batch, d_k, time2)

        attn_scores = torch.matmul(q, k)

        use_pos_scores = False
        if torch.jit.is_scripting() or torch.jit.is_tracing():
            # We can't put random.random() in the same line
            use_pos_scores = True
        elif not self.training or random.random() >= float(self.pos_emb_skip_rate):
            use_pos_scores = True

        if use_pos_scores:
            pos_emb = self.linear_pos(pos_emb)
            seq_len2 = 2 * seq_len - 1
            pos_emb = pos_emb.reshape(-1, seq_len2, num_heads, pos_head_dim).permute(
                2, 0, 3, 1
            )
            # pos shape now: (head, {1 or batch_size}, pos_dim, seq_len2)

            # (head, batch, time1, pos_dim) x (head, 1, pos_dim, seq_len2) -> (head,
            #  batch, time1, seq_len2) [where seq_len2 represents relative position.]
            pos_scores = torch.matmul(p, pos_emb)
            # the following .as_strided() expression converts the last axis of
            # pos_scores from relative to absolute position.  I don't know whether I
            # might have got the time-offsets backwards or not, but let this code define
            # which way round it is supposed to be.
            if torch.jit.is_tracing():
                (num_heads, batch_size, time1, n) = pos_scores.shape
                rows = torch.arange(start=time1 - 1, end=-1, step=-1)
                cols = torch.arange(seq_len)
                rows = rows.repeat(batch_size * num_heads).unsqueeze(-1)
                indexes = rows + cols
                pos_scores = pos_scores.reshape(-1, n)
                pos_scores = torch.gather(pos_scores, dim=1, index=indexes)
                pos_scores = pos_scores.reshape(num_heads, batch_size, time1, seq_len)
            else:
                pos_scores = pos_scores.as_strided(
                    (num_heads, batch_size, seq_len, seq_len),
                    (
                        pos_scores.stride(0),
                        pos_scores.stride(1),
                        pos_scores.stride(2) - pos_scores.stride(3),
                        pos_scores.stride(3),
                    ),
                    storage_offset=pos_scores.stride(3) * (seq_len - 1),
                )

            attn_scores = attn_scores + pos_scores

        if torch.jit.is_scripting() or torch.jit.is_tracing():
            pass
        elif self.training and random.random() < 0.1:
            # This is a harder way of limiting the attention scores to not be
            # too large.  It incurs a penalty if any of them has an absolute
            # value greater than 50.0.  this should be outside the normal range
            # of the attention scores.  We use this mechanism instead of, say,
            # something added to the loss function involving the entropy,
            # because once the entropy gets very small gradients through the
            # softmax can become very small, and we'd get zero derivatives.  The
            # choices of 1.0e-04 as the scale on the penalty makes this
            # mechanism vulnerable to the absolute scale of the loss function,
            # but we view this as a failsafe to avoid "implausible" parameter
            # values rather than a regularization method that should be active
            # under normal circumstances.
            attn_scores = penalize_abs_values_gt(
                attn_scores, limit=25.0, penalty=1.0e-04, name=self.name
            )

        assert attn_scores.shape == (num_heads, batch_size, seq_len, seq_len)

        if attn_mask is not None:
            assert attn_mask.dtype == torch.bool
            # use -1000 to avoid nan's where attn_mask and key_padding_mask make
            # all scores zero.  It's important that this be large enough that exp(-1000)
            # is exactly zero, for reasons related to const_attention_rate, it
            # compares the final weights with zero.
            attn_scores = attn_scores.masked_fill(attn_mask, -1000)

        if key_padding_mask is not None:
            assert key_padding_mask.shape == (
                batch_size,
                seq_len,
            ), key_padding_mask.shape
            attn_scores = attn_scores.masked_fill(
                key_padding_mask.unsqueeze(1),
                -1000,
            )

        # We use our own version of softmax, defined in scaling.py, which should
        # save a little of the memory used in backprop by, if we are in
        # automatic mixed precision mode (amp / autocast), by only storing the
        # half-precision output for backprop purposes.
        attn_weights = softmax(attn_scores, dim=-1)

        if torch.jit.is_scripting() or torch.jit.is_tracing():
            pass
        elif random.random() < 0.001 and not self.training:
            self._print_attn_entropy(attn_weights)

        attn_weights = nn.functional.dropout(
            attn_weights, p=self.dropout, training=self.training
        )

        return attn_weights

    def _print_attn_entropy(self, attn_weights: Tensor):
        # attn_weights: (num_heads, batch_size, seq_len, seq_len)
        (num_heads, batch_size, seq_len, seq_len) = attn_weights.shape

        with torch.no_grad():
            with torch.amp.autocast("cuda", enabled=False):
                attn_weights = attn_weights.to(torch.float32)
                attn_weights_entropy = (
                    -((attn_weights + 1.0e-20).log() * attn_weights)
                    .sum(dim=-1)
                    .mean(dim=(1, 2))
                )
                logging.debug(
                    f"name={self.name}, attn_weights_entropy = {attn_weights_entropy}"
                )


class SelfAttention(nn.Module):
    """
    The simplest possible attention module.  This one works with already-computed
    attention weights, e.g. as computed by RelPositionMultiheadAttentionWeights.

    Args:
          embed_dim: the input and output embedding dimension
          num_heads: the number of attention heads
          value_head_dim: the value dimension per head
    """

    def __init__(
        self,
        embed_dim: int,
        num_heads: int,
        value_head_dim: int,
    ) -> None:
        super().__init__()
        self.in_proj = nn.Linear(embed_dim, num_heads * value_head_dim, bias=True)

        self.out_proj = ScaledLinear(
            num_heads * value_head_dim,
            embed_dim,
            bias=True,
            initial_scale=0.05,
        )

        self.whiten = Whiten(
            num_groups=1,
            whitening_limit=_whitening_schedule(7.5, ratio=3.0),
            prob=(0.025, 0.25),
            grad_scale=0.01,
        )

    def forward(
        self,
        x: Tensor,
        attn_weights: Tensor,
    ) -> Tensor:
        """
        Args:
          x: input tensor, of shape (seq_len, batch_size, embed_dim)
         attn_weights: a tensor of shape (num_heads, batch_size, seq_len, seq_len),
          with seq_len being interpreted as (tgt_seq_len, src_seq_len).  Expect
          attn_weights.sum(dim=-1) == 1.
        Returns:
           a tensor with the same shape as x.
        """
        (seq_len, batch_size, embed_dim) = x.shape
        num_heads = attn_weights.shape[0]
        assert attn_weights.shape == (num_heads, batch_size, seq_len, seq_len)

        x = self.in_proj(x)  # (seq_len, batch_size, num_heads * value_head_dim)
        x = x.reshape(seq_len, batch_size, num_heads, -1).permute(2, 1, 0, 3)
        # now x: (num_heads, batch_size, seq_len, value_head_dim)
        value_head_dim = x.shape[-1]

        # todo: see whether there is benefit in overriding matmul
        x = torch.matmul(attn_weights, x)
        # v: (num_heads, batch_size, seq_len, value_head_dim)

        x = (
            x.permute(2, 1, 0, 3)
            .contiguous()
            .view(seq_len, batch_size, num_heads * value_head_dim)
        )

        # returned value is of shape (seq_len, batch_size, embed_dim), like the input.
        x = self.out_proj(x)
        x = self.whiten(x)

        return x


class FeedforwardModule(nn.Module):
    """Feedforward module in TTSZipformer model."""

    def __init__(self, embed_dim: int, feedforward_dim: int, dropout: FloatLike):
        super(FeedforwardModule, self).__init__()
        self.in_proj = nn.Linear(embed_dim, feedforward_dim)

        self.hidden_balancer = Balancer(
            feedforward_dim,
            channel_dim=-1,
            min_positive=0.3,
            max_positive=1.0,
            min_abs=0.75,
            max_abs=5.0,
        )

        # shared_dim=0 means we share the dropout mask along the time axis
        self.out_proj = ActivationDropoutAndLinear(
            feedforward_dim,
            embed_dim,
            activation="SwooshL",
            dropout_p=dropout,
            dropout_shared_dim=0,
            bias=True,
            initial_scale=0.1,
        )

        self.out_whiten = Whiten(
            num_groups=1,
            whitening_limit=_whitening_schedule(7.5),
            prob=(0.025, 0.25),
            grad_scale=0.01,
        )

    def forward(self, x: Tensor):
        x = self.in_proj(x)
        x = self.hidden_balancer(x)
        # out_proj contains SwooshL activation, then dropout, then linear.
        x = self.out_proj(x)
        x = self.out_whiten(x)
        return x


class NonlinAttention(nn.Module):
    """This is like the ConvolutionModule, but refactored so that we use multiplication
       by attention weights (borrowed from the attention module) in place of actual
       convolution.  We also took out the second nonlinearity, the one after the
       attention mechanism.

    Args:
        channels (int): The number of channels of conv layers.
    """

    def __init__(
        self,
        channels: int,
        hidden_channels: int,
    ) -> None:
        super().__init__()

        self.hidden_channels = hidden_channels

        self.in_proj = nn.Linear(channels, hidden_channels * 3, bias=True)

        # balancer that goes before the sigmoid.  Have quite a large min_abs value, at
        # 2.0, because we noticed that well-trained instances of this module have
        # abs-value before the sigmoid starting from about 3, and poorly-trained
        # instances of the module have smaller abs values before the sigmoid.
        self.balancer = Balancer(
            hidden_channels,
            channel_dim=-1,
            min_positive=ScheduledFloat((0.0, 0.25), (20000.0, 0.05)),
            max_positive=ScheduledFloat((0.0, 0.75), (20000.0, 0.95)),
            min_abs=0.5,
            max_abs=5.0,
        )
        self.tanh = nn.Tanh()

        self.identity1 = Identity()  # for diagnostics.
        self.identity2 = Identity()  # for diagnostics.
        self.identity3 = Identity()  # for diagnostics.

        self.out_proj = ScaledLinear(
            hidden_channels, channels, bias=True, initial_scale=0.05
        )

        self.whiten1 = Whiten(
            num_groups=1,
            whitening_limit=_whitening_schedule(5.0),
            prob=(0.025, 0.25),
            grad_scale=0.01,
        )

        self.whiten2 = Whiten(
            num_groups=1,
            whitening_limit=_whitening_schedule(5.0, ratio=3.0),
            prob=(0.025, 0.25),
            grad_scale=0.01,
        )

    def forward(
        self,
        x: Tensor,
        attn_weights: Tensor,
    ) -> Tensor:
        """.
        Args:
            x: a Tensor of shape (seq_len, batch_size, num_channels)
            attn_weights: a Tensor of shape (num_heads, batch_size, seq_len, seq_len)
        Returns:
            a Tensor with the same shape as x
        """
        x = self.in_proj(x)

        (seq_len, batch_size, _) = x.shape
        hidden_channels = self.hidden_channels

        s, x, y = x.chunk(3, dim=2)

        # s will go through tanh.

        s = self.balancer(s)
        s = self.tanh(s)

        s = s.unsqueeze(-1).reshape(seq_len, batch_size, hidden_channels)
        x = self.whiten1(x)
        x = x * s
        x = self.identity1(x)  # diagnostics only, it's the identity.

        (seq_len, batch_size, embed_dim) = x.shape
        num_heads = attn_weights.shape[0]
        assert attn_weights.shape == (num_heads, batch_size, seq_len, seq_len)

        x = x.reshape(seq_len, batch_size, num_heads, -1).permute(2, 1, 0, 3)
        # now x: (num_heads, batch_size, seq_len, head_dim)
        x = torch.matmul(attn_weights, x)
        # now x: (num_heads, batch_size, seq_len, head_dim)
        x = x.permute(2, 1, 0, 3).reshape(seq_len, batch_size, -1)

        y = self.identity2(y)
        x = x * y
        x = self.identity3(x)

        x = self.out_proj(x)
        x = self.whiten2(x)
        return x


class ConvolutionModule(nn.Module):
    """ConvolutionModule in Zipformer2 model.
    Modified from https://github.com/espnet/espnet/blob/master/espnet/nets/pytorch_backend/zipformer/convolution.py

    Args:
        channels (int): The number of channels of conv layers.
        kernel_size (int): Kernerl size of conv layers.
        bias (bool): Whether to use bias in conv layers (default=True).

    """

    def __init__(
        self,
        channels: int,
        kernel_size: int,
    ) -> None:
        """Construct a ConvolutionModule object."""
        super(ConvolutionModule, self).__init__()
        # kernerl_size should be a odd number for 'SAME' padding
        assert (kernel_size - 1) % 2 == 0

        bottleneck_dim = channels

        self.in_proj = nn.Linear(
            channels,
            2 * bottleneck_dim,
        )
        # the gradients on in_proj are a little noisy, likely to do with the
        # sigmoid in glu.

        # after in_proj we put x through a gated linear unit (nn.functional.glu). For
        # most layers the normal rms value of channels of x seems to be in the range 1
        # to 4, but sometimes, for some reason, for layer 0 the rms ends up being very
        # large, between 50 and 100 for different channels.  This will cause very peaky
        # and sparse derivatives for the sigmoid gating function, which will tend to
        # make the loss function not learn effectively.  (for most layers the average
        # absolute values are in the range 0.5..9.0, and the average p(x>0), i.e.
        # positive proportion, at the output of pointwise_conv1.output is around 0.35 to
        # 0.45 for different layers, which likely breaks down as 0.5 for the "linear"
        # half and 0.2 to 0.3 for the part that goes into the sigmoid.  The idea is that
        # if we constrain the rms values to a reasonable range via a constraint of
        # max_abs=10.0, it will be in a better position to start learning something,
        # i.e. to latch onto the correct range.
        self.balancer1 = Balancer(
            bottleneck_dim,
            channel_dim=-1,
            min_positive=ScheduledFloat((0.0, 0.05), (8000.0, 0.025)),
            max_positive=1.0,
            min_abs=1.5,
            max_abs=ScheduledFloat((0.0, 5.0), (8000.0, 10.0), default=1.0),
        )

        self.activation1 = Identity()  # for diagnostics

        self.sigmoid = nn.Sigmoid()

        self.activation2 = Identity()  # for diagnostics

        assert kernel_size % 2 == 1

        self.depthwise_conv = nn.Conv1d(
            in_channels=bottleneck_dim,
            out_channels=bottleneck_dim,
            groups=bottleneck_dim,
            kernel_size=kernel_size,
            padding=kernel_size // 2,
        )

        self.balancer2 = Balancer(
            bottleneck_dim,
            channel_dim=1,
            min_positive=ScheduledFloat((0.0, 0.1), (8000.0, 0.05)),
            max_positive=1.0,
            min_abs=ScheduledFloat((0.0, 0.2), (20000.0, 0.5)),
            max_abs=10.0,
        )

        self.whiten = Whiten(
            num_groups=1,
            whitening_limit=_whitening_schedule(7.5),
            prob=(0.025, 0.25),
            grad_scale=0.01,
        )

        self.out_proj = ActivationDropoutAndLinear(
            bottleneck_dim,
            channels,
            activation="SwooshR",
            dropout_p=0.0,
            initial_scale=0.05,
        )

    def forward(
        self,
        x: Tensor,
        src_key_padding_mask: Optional[Tensor] = None,
    ) -> Tensor:
        """Compute convolution module.

        Args:
            x: Input tensor (#time, batch, channels).
           src_key_padding_mask: the mask for the src keys per batch (optional):
               (batch, #time), contains True in masked positions.

        Returns:
            Tensor: Output tensor (#time, batch, channels).

        """

        x = self.in_proj(x)  # (time, batch, 2*channels)

        x, s = x.chunk(2, dim=2)
        s = self.balancer1(s)
        s = self.sigmoid(s)
        x = self.activation1(x)  # identity.
        x = x * s
        x = self.activation2(x)  # identity

        # (time, batch, channels)

        # exchange the temporal dimension and the feature dimension
        x = x.permute(1, 2, 0)  # (#batch, channels, time).

        if src_key_padding_mask is not None:
            x = x.masked_fill(src_key_padding_mask.unsqueeze(1).expand_as(x), 0.0)

        x = self.depthwise_conv(x)

        x = self.balancer2(x)
        x = x.permute(2, 0, 1)  # (time, batch, channels)

        x = self.whiten(x)  # (time, batch, channels)
        x = self.out_proj(x)  # (time, batch, channels)

        return x
