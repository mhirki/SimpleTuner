import torch, os, logging
from helpers.models.common import ImageModelFoundation, PredictionTypes, PipelineTypes, ModelTypes
from transformers import T5TokenizerFast, T5EncoderModel, CLIPTokenizer, CLIPTextModelWithProjection
from helpers.models.sd3.transformer import SD3Transformer2DModel
from helpers.models.sd3.pipeline import StableDiffusion3Pipeline, StableDiffusion3Img2ImgPipeline
from diffusers import AutoencoderKL
from diffusers.utils import convert_state_dict_to_diffusers
from peft.utils import get_peft_model_state_dict
from helpers.training.multi_process import _get_rank

logger = logging.getLogger(__name__)
logger.setLevel(os.environ.get("SIMPLETUNER_LOG_LEVEL", "INFO") if _get_rank() == 0 else "ERROR")

def _encode_sd3_prompt_with_t5(
    text_encoder,
    tokenizer,
    prompt=None,
    num_images_per_prompt=1,
    device=None,
    zero_padding_tokens: bool = True,
    max_sequence_length: int = 77,
):
    prompt = [prompt] if isinstance(prompt, str) else prompt
    batch_size = len(prompt)

    text_inputs = tokenizer(
        prompt,
        padding="max_length",
        max_length=max_sequence_length,
        truncation=True,
        add_special_tokens=True,
        return_tensors="pt",
    )
    text_input_ids = text_inputs.input_ids
    prompt_embeds = text_encoder(text_input_ids.to(device))[0]

    dtype = text_encoder.dtype
    prompt_embeds = prompt_embeds.to(dtype=dtype, device=device)

    _, seq_len, _ = prompt_embeds.shape

    # duplicate text embeddings and attention mask for each generation per prompt, using mps friendly method
    prompt_embeds = prompt_embeds.repeat(1, num_images_per_prompt, 1)
    prompt_embeds = prompt_embeds.view(batch_size * num_images_per_prompt, seq_len, -1)
    attention_mask = text_inputs.attention_mask.to(device)

    if zero_padding_tokens:
        # for some reason, SAI's reference code doesn't bother to mask the prompt embeddings.
        # this can lead to a problem where the model fails to represent short and long prompts equally well.
        # additionally, the model learns the bias of the prompt embeds' noise.
        return prompt_embeds * attention_mask.unsqueeze(-1).expand(prompt_embeds.shape)
    else:
        return prompt_embeds

def _encode_sd3_prompt_with_clip(
    text_encoder,
    tokenizer,
    prompt: str,
    device=None,
    num_images_per_prompt: int = 1,
    max_token_length: int = 77,
):
    prompt = [prompt] if isinstance(prompt, str) else prompt
    batch_size = len(prompt)

    text_inputs = tokenizer(
        prompt,
        padding="max_length",
        max_length=max_token_length,
        truncation=True,
        return_tensors="pt",
    )
    text_input_ids = text_inputs.input_ids
    prompt_embeds = text_encoder(text_input_ids.to(device), output_hidden_states=True)

    pooled_prompt_embeds = prompt_embeds[0]
    prompt_embeds = prompt_embeds.hidden_states[-2]
    prompt_embeds = prompt_embeds.to(dtype=text_encoder.dtype, device=device)

    _, seq_len, _ = prompt_embeds.shape
    # duplicate text embeddings for each generation per prompt, using mps friendly method
    prompt_embeds = prompt_embeds.repeat(1, num_images_per_prompt, 1)
    prompt_embeds = prompt_embeds.view(batch_size * num_images_per_prompt, seq_len, -1)

    return prompt_embeds, pooled_prompt_embeds

class SD3(ImageModelFoundation):
    PREDICTION_TYPE = PredictionTypes.FLOW_MATCHING
    MODEL_TYPE = ModelTypes.TRANSFORMER
    AUTOENCODER_CLASS = AutoencoderKL
    LATENT_CHANNEL_COUNT = 16
    # The safe diffusers default value for LoRA training targets.
    DEFAULT_LORA_TARGET = ["to_k", "to_q", "to_v", "to_out.0"]
    # Only training the Attention blocks by default seems to help more with SD3.
    DEFAULT_LYCORIS_TARGET = ["Attention"]

    MODEL_CLASS = SD3Transformer2DModel
    PIPELINE_CLASSES = {
        PipelineTypes.TEXT2IMG: StableDiffusion3Pipeline,
        PipelineTypes.IMG2IMG: StableDiffusion3Img2ImgPipeline,
    }
    MODEL_SUBFOLDER = "transformer"
    # The default model flavor to use when none is specified.
    DEFAULT_MODEL_FLAVOR = "medium"
    HUGGINGFACE_PATHS = {
        "medium": "stabilityai/stable-diffusion-3.5-medium",
        "large": "stabilityai/stable-diffusion-3.5-large",
    }

    TEXT_ENCODER_CONFIGURATION = {
        "text_encoder": {
            "name": "CLIP-L/14",
            "tokenizer": CLIPTokenizer,
            "tokenizer_subfolder": "tokenizer",
            "model": CLIPTextModelWithProjection,
        },
        "text_encoder_2": {
            "name": "CLIP-G/14",
            "tokenizer": CLIPTokenizer,
            "subfolder": "text_encoder_2",
            "tokenizer_subfolder": "tokenizer_2",
            "model": CLIPTextModelWithProjection,
        },
        "text_encoder_3": {
            "name": "T5 XXL v1.1",
            "tokenizer": T5TokenizerFast,
            "subfolder": "text_encoder_3",
            "tokenizer_subfolder": "tokenizer_3",
            "model": T5EncoderModel,
        },
    }

    def _format_text_embedding(self, text_embedding: torch.Tensor):
        """
        Models can optionally format the stored text embedding, eg. in a dict, or
        filter certain outputs from appearing in the file cache.

        self.config:
            text_embedding (torch.Tensor): The embed to adjust.
        
        Returns:
            torch.Tensor: The adjusted embed. By default, this method does nothing.
        """
        prompt_embeds, pooled_prompt_embeds = text_embedding

        return {
            "prompt_embeds": prompt_embeds,
            "pooled_prompt_embeds": pooled_prompt_embeds.squeeze(0),
        }

    def convert_text_embed_for_pipeline(self, text_embedding: torch.Tensor) -> dict:
        # logger.info(f"Converting embeds with shapes: {text_embedding['prompt_embeds'].shape} {text_embedding['pooled_prompt_embeds'].shape}")
        return {
            "prompt_embeds": text_embedding["prompt_embeds"].unsqueeze(0),
            "pooled_prompt_embeds": text_embedding["pooled_prompt_embeds"].unsqueeze(0),
        }

    def convert_negative_text_embed_for_pipeline(self, text_embedding: torch.Tensor, prompt: str) -> dict:
        # logger.info(f"Converting embeds with shapes: {text_embedding['prompt_embeds'].shape} {text_embedding['pooled_prompt_embeds'].shape}")
        return {
            "negative_prompt_embeds": text_embedding["prompt_embeds"].unsqueeze(0),
            "negative_pooled_prompt_embeds": text_embedding["pooled_prompt_embeds"].unsqueeze(0),
        }

    def _encode_prompts(self, prompts: list):
        """
        Encode a prompt for an SD3 model.

        Args:
            text_encoders: List of text encoders.
            tokenizers: List of tokenizers.
            prompt: The prompt to encode.
            num_images_per_prompt: The number of images to generate per prompt.
            is_validation: Whether the prompt is for validation. No-op for SD3.

        Returns:
            Tuple of (prompt_embeds, pooled_prompt_embeds).
        """
        num_images_per_prompt = 1

        clip_tokenizers = self.tokenizers[:2]
        clip_text_encoders = self.text_encoders[:2]

        clip_prompt_embeds_list = []
        clip_pooled_prompt_embeds_list = []
        for tokenizer, text_encoder in zip(clip_tokenizers, clip_text_encoders):
            prompt_embeds, pooled_prompt_embeds = _encode_sd3_prompt_with_clip(
                text_encoder=text_encoder,
                tokenizer=tokenizer,
                prompt=prompts,
                device=self.accelerator.device,
                num_images_per_prompt=num_images_per_prompt,
            )
            clip_prompt_embeds_list.append(prompt_embeds)
            clip_pooled_prompt_embeds_list.append(pooled_prompt_embeds)

        clip_prompt_embeds = torch.cat(clip_prompt_embeds_list, dim=-1)
        pooled_prompt_embeds = torch.cat(clip_pooled_prompt_embeds_list, dim=-1)
        zero_padding_tokens=(
            True if self.config.t5_padding == "zero" else False
        )
        t5_prompt_embed = _encode_sd3_prompt_with_t5(
            self.text_encoders[-1],
            self.tokenizers[-1],
            prompt=prompts,
            num_images_per_prompt=num_images_per_prompt,
            device=self.accelerator.device,
            zero_padding_tokens=zero_padding_tokens,
            max_sequence_length=self.config.tokenizer_max_length,
        )

        clip_prompt_embeds = torch.nn.functional.pad(
            clip_prompt_embeds,
            (0, t5_prompt_embed.shape[-1] - clip_prompt_embeds.shape[-1]),
        )
        prompt_embeds = torch.cat([clip_prompt_embeds, t5_prompt_embed], dim=-2)

        return prompt_embeds, pooled_prompt_embeds

    def model_predict(self, prepared_batch):
        logger.debug(
            "Input shapes:"
            f"\n{prepared_batch['noisy_latents'].shape}"
            f"\n{prepared_batch['timesteps'].shape}"
            f"\n{prepared_batch['encoder_hidden_states'].shape}"
            f"\n{prepared_batch['add_text_embeds'].shape}"
        )
        return {
            "model_prediction": self.model(
                hidden_states=prepared_batch["noisy_latents"].to(
                    device=self.accelerator.device,
                    dtype=self.config.base_weight_dtype,
                ),
                timestep=prepared_batch["timesteps"],
                encoder_hidden_states=prepared_batch["encoder_hidden_states"].to(
                    device=self.accelerator.device,
                    dtype=self.config.base_weight_dtype,
                ),
                pooled_projections=prepared_batch["add_text_embeds"].to(
                    device=self.accelerator.device,
                    dtype=self.config.weight_dtype,
                ),
                return_dict=False,
            )[0]
        }
    
    def load_lora_weights(self, models, input_dir):
        unet_ = None
        transformer_ = None
        denoiser = None
        text_encoder_one_ = None
        text_encoder_two_ = None

        while len(models) > 0:
            model = models.pop()

            if isinstance(
                self.unwrap_model(model=model),
                type(self.unwrap_model(model=self.transformer)),
            ):
                transformer_ = model
                denoiser = transformer_
            elif isinstance(
                self.unwrap_model(model=model),
                type(self.unwrap_model(model=self.text_encoders[0])),
            ):
                text_encoder_one_ = model
            elif isinstance(
                self.unwrap_model(model=model),
                type(self.unwrap_model(model=self.text_encoders[1])),
            ):
                text_encoder_two_ = model
            else:
                raise ValueError(
                    f"unexpected save model: {model.__class__}"
                    f"\nunwrapped: {self.unwrap_model(model=model).__class__}"
                    f"\nunet: {self.unwrap_model(model=self.get_trained_component()).__class__}"
                )

        key_to_replace = self.model.MODEL_SUBFOLDER
        lora_state_dict = self.pipeline_class.lora_state_dict(input_dir)

        denoiser_state_dict = {
            f'{k.replace(f"{key_to_replace}.", "")}': v
            for k, v in lora_state_dict.items()
            if k.startswith(f"{key_to_replace}.")
        }
        denoiser_state_dict = convert_unet_state_dict_to_peft(denoiser_state_dict)
        incompatible_keys = set_peft_model_state_dict(
            denoiser, denoiser_state_dict, adapter_name="default"
        )

        if incompatible_keys is not None:
            # check only for unexpected keys
            unexpected_keys = getattr(incompatible_keys, "unexpected_keys", None)
            if unexpected_keys:
                logger.warning(
                    f"Loading adapter weights from state_dict led to unexpected keys not found in the model: "
                    f" {unexpected_keys}. "
                )

        if self.args.train_text_encoder:
            # Do we need to call `scale_lora_layers()` here?
            from diffusers.training_utils import _set_state_dict_into_text_encoder

            _set_state_dict_into_text_encoder(
                lora_state_dict,
                prefix="text_encoder.",
                text_encoder=text_encoder_one_,
            )

            _set_state_dict_into_text_encoder(
                lora_state_dict,
                prefix="text_encoder_2.",
                text_encoder=text_encoder_two_,
            )

    def check_user_config(self):
        """
        Checks self.config values against important issues. Optionally implemented in child class.
        """
        if self.config.base_model_precision == "fp8-quanto":
            raise ValueError(
                "SD3 does not support fp8-quanto. Please use fp8-torchao or int8 precision level instead."
            )
        t5_max_length = 154
        if self.config.tokenizer_max_length is None or int(self.config.tokenizer_max_length) > t5_max_length:
            if not self.config.i_know_what_i_am_doing:
                logger.warning(
                    f"Updating T5 XXL tokeniser max length to {t5_max_length} for SD3."
                )
                self.config.tokenizer_max_length = t5_max_length
            else:
                logger.warning(
                    f"-!- SD3 supports a max length of {t5_max_length} tokens, but you have supplied `--i_know_what_i_am_doing`, so this limit will not be enforced. -!-"
                )
                logger.warning(
                    f"The model will begin to collapse after a short period of time, if the model you are continuing from has not been tuned beyond {t5_max_length} tokens."
                )
        # Disable custom VAEs for SD3.
        self.config.pretrained_vae_model_name_or_path = None
        # Disable Compel.
        self.config.disable_compel = True
        if self.config.aspect_bucket_alignment != 64:
            logger.warning(
                "MM-DiT requires an alignment value of 64px. Overriding the value of --aspect_bucket_alignment."
            )
            self.config.aspect_bucket_alignment = 64
        if self.config.sd3_t5_uncond_behaviour is None:
            self.config.sd3_t5_uncond_behaviour = self.config.sd3_clip_uncond_behaviour
        logger.info(
            f"SD3 embeds for unconditional captions: t5={self.config.sd3_t5_uncond_behaviour}, clip={self.config.sd3_clip_uncond_behaviour}"
        )
