import torch
import gradio as gr
from audiocraft.models.musicgen import MusicGen
from audiocraft.models.audiogen import AudioGen
from audiocraft.models.encodec import InterleaveStereoCompressionModel
from einops import rearrange
from typing import Optional, Tuple, TypedDict
import numpy as np
import os
from src.bark.npz_tools import save_npz_musicgen
from src.musicgen.setup_seed_ui_musicgen import setup_seed_ui_musicgen
from src.bark.parse_or_set_seed import parse_or_set_seed
from src.musicgen.audio_array_to_sha256 import audio_array_to_sha256
from src.utils.set_seed import set_seed

from src.extensions_loader.ext_callback_save_generation import (
    ext_callback_save_generation_musicgen,
)
from src.utils.create_base_filename import create_base_filename
from src.history_tab.save_to_favorites import save_to_favorites
from src.bark.get_filenames import get_filenames
from src.utils.date import get_date_string
from scipy.io.wavfile import write as write_wav
from src.utils.save_waveform_plot import save_waveform_plot

import json
from typing import Optional
from importlib.metadata import version

AUDIOCRAFT_VERSION = version("audiocraft")


class MusicGenGeneration(TypedDict):
    model: str
    text: str
    melody: Optional[Tuple[int, np.ndarray]]
    duration: float
    topk: int
    topp: float
    temperature: float
    cfg_coef: float
    seed: int
    use_multi_band_diffusion: bool


def melody_to_sha256(melody: Optional[Tuple[int, np.ndarray]]) -> Optional[str]:
    if melody is None:
        return None
    sr, audio_array = melody
    return audio_array_to_sha256(audio_array)


def generate_and_save_metadata(
    prompt: str,
    date: str,
    filename_json: str,
    params: MusicGenGeneration,
    audio_array: np.ndarray,
):
    metadata = {
        "_version": "0.0.1",
        "_hash_version": "0.0.3",
        "_type": "musicgen",
        "_audiocraft_version": AUDIOCRAFT_VERSION,
        "models": {},
        "prompt": prompt,
        "hash": audio_array_to_sha256(audio_array),
        "date": date,
        **params,
        "seed": str(params["seed"]),
        "melody": melody_to_sha256(params.get("melody", None)),
    }
    with open(filename_json, "w") as outfile:
        json.dump(metadata, outfile, indent=2)

    return metadata


def save_generation(
    audio_array: np.ndarray,
    SAMPLE_RATE: int,
    params: MusicGenGeneration,
    tokens: Optional[torch.Tensor] = None,
):
    prompt = params["text"]
    date = get_date_string()
    title = prompt[:20].replace(" ", "_")
    base_filename = create_base_filename(title, "outputs", model="musicgen", date=date)

    filename, filename_png, filename_json, filename_npz = get_filenames(base_filename)
    stereo = audio_array.shape[0] == 2
    if stereo:
        audio_array = np.transpose(audio_array)
    write_wav(filename, SAMPLE_RATE, audio_array)
    plot = save_waveform_plot(audio_array, filename_png)

    metadata = generate_and_save_metadata(
        prompt=prompt,
        date=date,
        filename_json=filename_json,
        params=params,
        audio_array=audio_array,
    )
    if tokens is not None:
        save_npz_musicgen(filename_npz, tokens, metadata)

    filename_ogg = filename.replace(".wav", ".ogg")
    ext_callback_save_generation_musicgen(
        audio_array=audio_array,
        files={
            "wav": filename,
            "png": filename_png,
            "ogg": filename_ogg,
        },
        metadata=metadata,
        SAMPLE_RATE=SAMPLE_RATE,
    )

    return filename, plot, metadata


MODEL = None


def load_model(version):
    if version == "facebook/audiogen-medium":
        return AudioGen.get_pretrained(version)
    print("Loading model", version)
    return MusicGen.get_pretrained(version)


def log_generation_musicgen(
    params: MusicGenGeneration,
):
    print("Generating: '''", params["text"], "'''")
    print("Parameters:")
    for key, value in params.items():
        print(key, ":", value)


def generate(params: MusicGenGeneration, melody_in: Optional[Tuple[int, np.ndarray]]):
    model = params["model"]
    text = params["text"]
    # due to JSON serialization limitations
    params["melody"] = None if "melody" not in model else melody_in
    melody = params["melody"]

    global MODEL
    if MODEL is None or MODEL.name != model:
        MODEL = load_model(model)

    MODEL.set_generation_params(
        use_sampling=True,
        top_k=params["topk"],
        top_p=params["topp"],
        temperature=params["temperature"],
        cfg_coef=params["cfg_coef"],
        duration=params["duration"],
    )

    tokens = None

    import time

    start = time.time()

    params["seed"] = parse_or_set_seed(params["seed"], 0)
    # generator = torch.Generator(device=MODEL.device).manual_seed(params["seed"])
    log_generation_musicgen(params)
    if "melody" in model and melody is not None:
        sr, melody = melody[0], torch.from_numpy(melody[1]).to(
            MODEL.device
        ).float().t().unsqueeze(0)
        print(melody.shape)
        if melody.dim() == 2:
            melody = melody[None]
        melody = melody[..., : int(sr * MODEL.lm.cfg.dataset.segment_duration)]  # type: ignore
        output, tokens = MODEL.generate_with_chroma(
            descriptions=[text],
            melody_wavs=melody,
            melody_sample_rate=sr,
            progress=True,
            return_tokens=True,
            # generator=generator,
        )
    elif model == "facebook/audiogen-medium":
        output = MODEL.generate(
            descriptions=[text],
            progress=True,
            # generator=generator,
        )
    else:
        output, tokens = MODEL.generate(
            descriptions=[text],
            progress=True,
            return_tokens=True,
            # generator=generator,
        )
    set_seed(-1)

    elapsed = time.time() - start
    # print time taken
    print("Generated in", "{:.3f}".format(elapsed), "seconds")

    if params["use_multi_band_diffusion"]:
        if model != "facebook/audiogen-medium":
            from audiocraft.models.multibanddiffusion import MultiBandDiffusion

            mbd = MultiBandDiffusion.get_mbd_musicgen()
            if isinstance(MODEL.compression_model, InterleaveStereoCompressionModel):
                left, right = MODEL.compression_model.get_left_right_codes(tokens)
                tokens = torch.cat([left, right])
            outputs_diffusion = mbd.tokens_to_wav(tokens)
            if isinstance(MODEL.compression_model, InterleaveStereoCompressionModel):
                assert outputs_diffusion.shape[1] == 1  # output is mono
                outputs_diffusion = rearrange(
                    outputs_diffusion, "(s b) c t -> b (s c) t", s=2
                )
            output = outputs_diffusion.detach().cpu().numpy().squeeze()
        else:
            print("NOTICE: Multi-band diffusion is not supported for AudioGen")
            params["use_multi_band_diffusion"] = False
            output = output.detach().cpu().numpy().squeeze()
    else:
        output = output.detach().cpu().numpy().squeeze()

    filename, plot, _metadata = save_generation(
        audio_array=output,
        SAMPLE_RATE=MODEL.sample_rate,
        params=params,
        tokens=tokens,
    )

    return [
        (MODEL.sample_rate, output.transpose()),
        os.path.dirname(filename),
        plot,
        params["seed"],
        _metadata,
    ]


def generation_tab_musicgen():
    with gr.Tab("MusicGen + AudioGen"):
        gr.Markdown(f"""Audiocraft version: {AUDIOCRAFT_VERSION}""")
        with gr.Row(equal_height=False):
            with gr.Column():
                text = gr.Textbox(
                    label="Prompt", lines=3, placeholder="Enter text here..."
                )
                model = gr.Radio(
                    [
                        "facebook/musicgen-melody",
                        "facebook/musicgen-medium",
                        "facebook/musicgen-small",
                        "facebook/musicgen-large",
                        "facebook/audiogen-medium",
                        "facebook/musicgen-melody-large",
                        "facebook/musicgen-stereo-small",
                        "facebook/musicgen-stereo-medium",
                        "facebook/musicgen-stereo-melody",
                        "facebook/musicgen-stereo-large",
                        "facebook/musicgen-stereo-melody-large",
                    ],
                    label="Model",
                    value="facebook/musicgen-small",
                )
                melody = gr.Audio(
                    source="upload",
                    type="numpy",
                    label="Melody (optional)",
                    elem_classes="tts-audio",
                )
                submit = gr.Button("Generate", variant="primary")
            with gr.Column():
                duration = gr.Slider(
                    minimum=1,
                    maximum=360,
                    value=10,
                    label="Duration",
                )
                with gr.Row():
                    topk = gr.Number(label="Top-k", value=250, interactive=True)
                    topp = gr.Slider(
                        minimum=0.0,
                        maximum=1.5,
                        value=0.0,
                        label="Top-p",
                        step=0.05,
                    )
                    temperature = gr.Slider(
                        minimum=0.0,
                        maximum=1.5,
                        value=1.0,
                        label="Temperature",
                        step=0.05,
                    )
                    cfg_coef = gr.Slider(
                        minimum=0.0,
                        maximum=10.0,
                        value=3.0,
                        label="Classifier Free Guidance",
                        step=0.1,
                    )
                use_multi_band_diffusion = gr.Checkbox(
                    label="Use Multi-Band Diffusion (High VRAM Usage)",
                    value=False,
                )
                seed, set_old_seed_button, _ = setup_seed_ui_musicgen()

        with gr.Column():
            output = gr.Audio(
                label="Generated Music",
                type="numpy",
                interactive=False,
                elem_classes="tts-audio",
            )
            image = gr.Image(label="Waveform", shape=(None, 100), elem_classes="tts-image")  # type: ignore
            with gr.Row():
                history_bundle_name_data = gr.Textbox(visible=False)
                save_button = gr.Button("Save to favorites", visible=True)
                melody_button = gr.Button("Use as melody", visible=True)
            save_button.click(
                fn=save_to_favorites,
                inputs=[history_bundle_name_data],
                outputs=[save_button],
            )

            melody_button.click(
                fn=lambda melody_in: melody_in,
                inputs=[output],
                outputs=[melody],
            )

    inputs = [
        text,
        melody,
        model,
        duration,
        topk,
        topp,
        temperature,
        cfg_coef,
        seed,
        use_multi_band_diffusion,
    ]

    def update_json(
        text,
        _melody,
        model,
        duration,
        topk,
        topp,
        temperature,
        cfg_coef,
        seed,
        use_multi_band_diffusion,
    ):
        return {
            "text": text,
            "melody": "exists" if _melody else "None",  # due to JSON limits
            "model": model,
            "duration": float(duration),
            "topk": int(topk),
            "topp": float(topp),
            "temperature": float(temperature),
            "cfg_coef": float(cfg_coef),
            "seed": int(seed),
            "use_multi_band_diffusion": bool(use_multi_band_diffusion),
        }

    seed_cache = gr.State()  # type: ignore
    result_json = gr.JSON(
        visible=False,
    )

    set_old_seed_button.click(
        fn=lambda x: gr.Number.update(value=x),
        inputs=seed_cache,
        outputs=seed,
    )

    submit.click(
        inputs=inputs,
        fn=lambda text, melody, model, duration, topk, topp, temperature, cfg_coef, seed, use_multi_band_diffusion: generate(
            params=update_json(
                text,
                melody,
                model,
                duration,
                topk,
                topp,
                temperature,
                cfg_coef,
                seed,
                use_multi_band_diffusion,
            ),  # type: ignore
            melody_in=melody,
        ),
        outputs=[output, history_bundle_name_data, image, seed_cache, result_json],
        api_name="musicgen",
    )


if __name__ == "__main__":
    with gr.Blocks() as demo:
        generation_tab_musicgen()

    demo.launch()

# from src.musicgen.musicgen_tab import generate, MusicGenGeneration

# generate(
#     params=MusicGenGeneration(
#         model="facebook/musicgen-small",
#         text="I am a robot",
#         cfg_coef=3.0,
#         duration=10,
#         melody=None,
#         seed=0,
#         temperature=1.0,
#         topk=250,
#         topp=0.0,
#         use_multi_band_diffusion=False,
#     ),
#     melody_in=None,
# )
