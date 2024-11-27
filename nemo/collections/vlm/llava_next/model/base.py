# Copyright (c) 2024, NVIDIA CORPORATION.  All rights reserved.
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

import math
from typing import Callable, Dict, List, Optional

import torch
import torch.distributed
from megatron.core import parallel_state as ps
from megatron.core.inference_params import InferenceParams
from megatron.core.tensor_parallel import gather_from_sequence_parallel_region

from nemo.collections.llm.gpt.model.base import get_batch_on_this_context_parallel_rank, get_packed_seq_params
from nemo.collections.vlm import MCoreNevaModel, NevaConfig
from nemo.collections.vlm.llava_next.model.utils import merge_input_ids_with_image_features, pack_image_features
from nemo.collections.vlm.neva.data.multimodal_tokens import IMAGE_TOKEN_INDEX


def llava_next_data_step(dataloader_iter) -> Dict[str, torch.Tensor]:
    """
    Processes a batch of data from the dataloader for the LLaVA Next model.

    Args:
        dataloader_iter (Iterator): An iterator that provides batches of data from the dataloader.

    Returns:
        Dict[str, torch.Tensor]: A dictionary containing the processed batch, ready for input into the model.

    Notes:
        - Filters and moves required keys to the appropriate device.
        - Slices the batch along the sequence dimension for context parallelism.
    """
    from megatron.core import parallel_state

    # Based on: https://github.com/NVIDIA/Megatron-LM/blob/main/pretrain_gpt.py#L87
    # https://github.com/NVIDIA/NeMo/blob/main/nemo/collections/nlp/models/language_modeling/
    # megatron_gpt_model.py#L828-L842
    batch = next(dataloader_iter)
    _batch: dict
    if isinstance(batch, tuple) and len(batch) == 3:
        _batch = batch[0]
    else:
        _batch = batch

    required_keys = set()
    required_keys.update(
        (
            "tokens",
            "attention_mask",
            "media",
            "num_media_tiles",
            "image_sizes",
        )
    )
    if parallel_state.is_pipeline_first_stage():
        required_keys.update(("position_ids",))
    if parallel_state.is_pipeline_last_stage():
        required_keys.update(
            (
                "labels",
                "loss_mask",
            )
        )

    _batch = {
        key: val.cuda(non_blocking=True) if key in required_keys and val is not None else None
        for key, val in _batch.items()
    }
    # slice batch along sequence dimension for context parallelism
    output = get_batch_on_this_context_parallel_rank(_batch)

    return output


def llava_next_forward_step(model, batch) -> torch.Tensor:
    """
    Performs the forward step for the LLaVA Next model.

    Args:
        model (torch.nn.Module): The LLaVA Next model instance.
        batch (Dict[str, torch.Tensor]): A dictionary containing input tensors for the forward step.

    Returns:
        torch.Tensor: The output from the model's forward computation.

    Notes:
        - Constructs the forward arguments based on the provided batch.
        - Includes optional parameters like packed sequence parameters if available.
    """
    forward_args = {
        "media": batch["media"],
        "input_ids": batch["tokens"],
        "position_ids": batch["position_ids"],
        "attention_mask": batch.get("attention_mask", None),
        "loss_mask": batch.get("loss_mask", None),
        "labels": batch.get("labels", None),
        "image_sizes": batch.get("image_sizes", None),
        "num_media_tiles": batch.get("num_media_tiles", None),
    }

    if 'cu_seqlens' in batch:
        forward_args['packed_seq_params'] = get_packed_seq_params(batch)
    return model(**forward_args)


class LlavaNextConfig(NevaConfig):
    """
    Configuration class for the LLaVA Next model.
    Overrides NevaConfig and modifies forward and data step fn.

    """

    forward_step_fn: Callable = llava_next_forward_step
    data_step_fn: Callable = llava_next_data_step

    def configure_model(self, tokenizer) -> "MCoreLlavaNextModel":
        """
        Configures the LLaVA Next model with the appropriate settings.

        Args:
            tokenizer: Tokenizer instance to be used with the model.

        Returns:
            MCoreLlavaNextModel: An instance of the LLaVA Next model.
        """

        self.language_transformer_config.tensor_model_parallel_size = self.tensor_model_parallel_size
        self.language_transformer_config.sequence_parallel = self.sequence_parallel
        self.vision_transformer_config.tensor_model_parallel_size = self.tensor_model_parallel_size
        self.vision_projection_config.tensor_model_parallel_size = self.tensor_model_parallel_size
        self.language_transformer_config.pipeline_model_parallel_size = self.pipeline_model_parallel_size

        if self.encoder_pipeline_model_parallel_size > 0:
            assert self.encoder_pipeline_model_parallel_size == 1, "ViT can only live on 1 pipeline stage."
            self.vision_transformer_config.pipeline_model_parallel_size = self.encoder_pipeline_model_parallel_size
            self.vision_projection_config.pipeline_model_parallel_size = self.encoder_pipeline_model_parallel_size
            self.language_transformer_config.encoder_pipeline_model_parallel_size = (
                self.encoder_pipeline_model_parallel_size
            )
            if self.encoder_tensor_model_parallel_size > 0:
                self.vision_transformer_config.tensor_model_parallel_size = self.encoder_tensor_model_parallel_size
                self.vision_projection_config.tensor_model_parallel_size = self.encoder_tensor_model_parallel_size

        model = MCoreLlavaNextModel(
            config=self,
            tokenizer=tokenizer,
            pre_process=ps.is_pipeline_first_stage()
            or ps.get_pipeline_model_parallel_rank() == self.encoder_pipeline_model_parallel_size,
            post_process=ps.is_pipeline_last_stage(),
            add_encoder=ps.is_pipeline_first_stage(),
            add_decoder=ps.is_pipeline_last_stage()
            or ps.get_pipeline_model_parallel_rank() >= self.encoder_pipeline_model_parallel_size,
            drop_vision_class_token=self.drop_vision_class_token,
        )

        return model


class MCoreLlavaNextModel(MCoreNevaModel):
    """
    The LLaVA Next model class, extending MCoreNevaModel.

    Attributes:
        image_newline (torch.nn.Parameter): A learnable parameter for handling image newlines.
    """

    def __init__(
        self,
        config: LlavaNextConfig,
        tokenizer=None,
        pre_process: bool = True,
        post_process: bool = True,
        add_encoder: bool = True,
        add_decoder: bool = True,
        drop_vision_class_token: bool = False,
    ) -> None:
        """
        Initializes the LLaVA Next model.
        Calls the super class init and initialize image_newline parameter

        Args:
            config (LlavaNextConfig): Model configuration instance.
            tokenizer: Optional tokenizer instance.
            pre_process (bool): Whether to enable preprocessing.
            post_process (bool): Whether to enable postprocessing.
            add_encoder (bool): Whether to add the encoder module.
            add_decoder (bool): Whether to add the decoder module.
            drop_vision_class_token (bool): Whether to drop the vision class token.
        """
        super().__init__(
            config=config,
            tokenizer=tokenizer,
            pre_process=pre_process,
            post_process=post_process,
            add_encoder=add_encoder,
            add_decoder=add_decoder,
            drop_vision_class_token=drop_vision_class_token,
        )
        # extra image_newline learnable parameter for llava_next
        embed_std = 1 / math.sqrt(config.vision_projection_config.hidden_size)
        self.image_newline = torch.nn.Parameter(torch.randn(config.vision_projection_config.hidden_size) * embed_std)

    def forward(
        self,
        input_ids: torch.Tensor,
        position_ids: torch.Tensor,
        image_sizes: torch.Tensor,
        loss_mask: Optional[torch.Tensor] = None,
        attention_mask: Optional[torch.Tensor] = None,
        media: Optional[torch.Tensor] = None,
        labels: Optional[torch.Tensor] = None,
        inference_params: Optional[InferenceParams] = None,
        num_media_tiles: Optional[List[int]] = None,
        media_token_index: Optional[int] = IMAGE_TOKEN_INDEX,
        runtime_gather_output: Optional[bool] = None,
    ) -> torch.Tensor:
        """Forward function of the LLaVA Next model.

        Args:
            images (torch.Tensor): input image of shape [num_tiles, img_h, img_w].
                                    num_tiles means the number of image tiles in this batch.
            input_ids (torch.Tensor): input text ids [batch, text_seq_len].
            position_ids (torch.Tensor): input text position ids [batch, text_seq_len].
            image_sizes (torch.Tensor): Raw image sizes  before tiling (N,2).
            attention_mask (torch.Tensor): Attention mask for the language model [batch, text seq length].
            labels (torch.Tensor): Optional target text labels [batch, combined_seq_len].
            loss_mask (torch.Tensor): Text loss mask [batch, text_seq_len].
            inference_params (InferenceParams): Inference-time parameters including KV cache.
            num_media_tiles (list of int): Number of tiles per image. Default None assumes 1 tile per image.
            image_token_index (int): ID for input images.

        Returns:
            output (torch.Tensor): Loss ([b, s]) if labels are provided; logits ([b, s, vocab_size]) otherwise.
            loss_mask (torch.Tensor): Loss mask expanded to combined sequence length. Shape [b, s].
        """

        use_inference_kv_cache = (
            inference_params is not None and "image_tokens_count" in inference_params.key_value_memory_dict
        )
        has_images = media.shape[0] > 0

        # If running inference, we can skip media token computation
        # if they were computed already earlier for this sample.
        if use_inference_kv_cache:
            media_embeddings = None
        elif self.add_encoder and not has_images:
            # If no images provided, use an empty image embeddings tensor.
            media_embeddings = torch.tensor([], dtype=media.dtype, device=media.device).reshape(0, 0, 0)
        elif self.add_encoder and has_images:
            # media is in shape of (num_images_in_mbs, c, h, w)
            # note num_images_in_mbs is not mbs but total images in this mbs.
            if self.vision_model_from_hf:
                self.vision_model = self.vision_model.eval()
                media_embeddings = self.vision_model(media, output_hidden_states=True)
                media_embeddings = media_embeddings[-1][
                    self.config.vision_feature_layer
                ]  # [num_images, img_seq_len, h_vision]
            else:
                # TODO(yuya): MCore Clip path not yet support taking a specific layer hidden states
                media = media.to(next(self.vision_model.parameters()).dtype)
                media_embeddings = self.vision_model(media, num_unused_layers=-self.config.vision_feature_layer - 1)
            if self._drop_vision_class_token:
                class_token_len = getattr(self.vision_model, "class_token_len", 1)
                media_embeddings = media_embeddings[:, class_token_len:, :]

            # contiguous() required as `permute` can sparsify the tensor and this breaks pipelining
            media_embeddings = media_embeddings.permute(1, 0, 2).contiguous()  # [img_seq_len, num_tiles, h_vision]

            # map vision model output size to language model input size.
            media_embeddings = self.vision_projection(media_embeddings)  # [img_seq_len, num_tiles, h_language]

            # TODO: Support batched inference.
            # In inference, the language model KV cache will be updated for image token positions.
            # Store the image tokens sequence length to be used as an offset to the KV cache later.
            if inference_params is not None:
                inference_params.key_value_memory_dict["media_tokens_count"] = (
                    media_embeddings.shape[0] * media_embeddings.shape[1]
                )
        else:
            media_embeddings = self.encoder_hidden_state

        if not self.add_decoder:
            return media_embeddings

        language_embeddings = None
        if self.pre_process:
            input_ids_text = input_ids.clone()
            # MultiModal Token indices are assumed to be values
            input_ids_text[input_ids_text < 0] = 0
            # Note: This adds absolute position embedding but not RoPE.
            # Each image is counted as one position.
            # RoPE is added in language_model forward. Each image embedding is one position.
            if self.sequence_parallel_lm:
                # Pad to nearest multiple of TP world size for embedding.
                tp_world_size = ps.get_tensor_model_parallel_world_size()
                padded_seq_len = (
                    int((input_ids_text.shape[1] + tp_world_size - 1) // tp_world_size * tp_world_size)
                    - input_ids_text.shape[1]
                )
                if padded_seq_len != 0:
                    input_ids_text = torch.nn.functional.pad(input_ids_text, (0, padded_seq_len))
                    if position_ids is not None:
                        position_ids = torch.nn.functional.pad(position_ids, (0, padded_seq_len))
            language_embeddings = self.language_model.embedding(
                input_ids=input_ids_text, position_ids=position_ids
            )  # [text_seq_len, b, h_language]
            if self.sequence_parallel_lm:
                # Gather the language embeddings back.
                # We use the full embedding to insert image embeddings
                # and then scatter to avoid load imbalance.
                language_embeddings = gather_from_sequence_parallel_region(
                    language_embeddings, tensor_parallel_output_grad=False
                )
                # Remove the padding done for SP as we'll need new padding calculation
                # after image embeddings are inserted.
                if padded_seq_len != 0:
                    language_embeddings = language_embeddings[:-padded_seq_len]
            language_embeddings = language_embeddings.transpose(1, 0).contiguous()  # [b, text_seq_len, h_language]

        # Assume 1 tile per image if the number of tiles is not provided.
        if num_media_tiles is None:
            num_media_tiles = torch.ones(media.shape[0], dtype=torch.int, device=input_ids.device)
        elif isinstance(num_media_tiles, list):
            num_media_tiles = torch.tensor(num_media_tiles, dtype=torch.int, device=input_ids.device)

        media_embeddings = torch.split(media_embeddings, num_media_tiles.tolist(), dim=0)
        media_embeddings, feature_lens = pack_image_features(
            media_embeddings,
            image_sizes,
            vision_feature_select_strategy='default',
            image_newline=self.image_newline,
        )

        combined_embeddings, attention_mask, position_ids, final_labels, final_input_ids, final_loss_mask = (
            merge_input_ids_with_image_features(
                media_embeddings,
                feature_lens,
                language_embeddings,
                input_ids,
                attention_mask,
                position_ids,
                labels=labels,
                image_token_index=media_token_index,
            )
        )

        combined_embeddings = combined_embeddings.permute(1, 0, 2)
        combined_embeddings = combined_embeddings.contiguous()
        output = self.language_model(
            input_ids=None,
            position_ids=None,
            attention_mask=attention_mask,
            decoder_input=combined_embeddings,
            labels=final_labels,
            inference_params=inference_params,
            runtime_gather_output=runtime_gather_output,
        )

        if labels is None or loss_mask is None:
            return output

        return output, final_loss_mask.contiguous()


__all__ = [
    "LlavaNextConfig",
]