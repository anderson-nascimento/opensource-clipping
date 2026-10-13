"""
clipping.runner — Pipeline Orchestrator

Maps to Cell 4 (Execute) of the notebook.
Orchestrates the full clip generation pipeline.
"""

import json
import os

from . import diarization as diarization_mod
from . import engine, metadata, studio, hook_manager, voiceover


def run_pipeline(cfg, whisper_model_instance=None) -> list[dict]:
    """
    Run the full clipping pipeline:
      1. Download YouTube video
      2. Transcribe with Whisper
      3. Analyse with Gemini AI
      4. Normalize metadata
      5. Prepare glitch transition
      6. Render each clip
      7. Save render_manifest.json

    Parameters
    ----------
    cfg : SimpleNamespace
        Configuration object from ``config.build_config()``.
    whisper_model_instance : WhisperModel or None
        Optional pre-loaded WhisperModel to reuse across batch runs.

    Returns
    -------
    list[dict]
        Render manifest (one dict per clip).
    """

    # Step 1 — Download
    source_platform = getattr(cfg, "source_platform", "youtube")
    engine.download_video(
        cfg.url_youtube,
        cfg.original_video_file,
        getattr(cfg, "use_dlp_subs", False),
        getattr(cfg, "download_source_height", "max"),
        source_platform=source_platform,
        yt_cookies=getattr(cfg, "yt_cookies", None),
    )

    # Step 2 — Transcribe
    full_transcript = ""
    transcript_segments = []

    import glob

    # Mencari file json3 apapun (karena bahasanya bisa .id.json3 atau .en.json3)
    json3_files = glob.glob(cfg.original_video_file.replace(".mp4", ".*.json3"))
    file_json3 = json3_files[0] if json3_files else None

    # Only run YouTube JSON3 subtitle search for YouTube sources
    if source_platform == "youtube":
        if (
            getattr(cfg, "use_dlp_subs", False)
            and file_json3
            and os.path.exists(file_json3)
        ):
            full_transcript, transcript_segments = engine.parse_youtube_json3_subs(
                file_json3, max_words_per_subtitle=cfg.max_words_per_subtitle
            )
            if full_transcript and transcript_segments:
                print(
                    f"✅ Successfully parsed YouTube subtitles ({os.path.basename(file_json3)}), skipping Whisper."
                )

    if not full_transcript or not transcript_segments:
        full_transcript, transcript_segments = engine.transcribe_video(
            cfg.original_video_file,
            max_words_per_subtitle=cfg.max_words_per_subtitle,
            model_size=cfg.whisper_model,
            device=cfg.whisper_device,
            compute_type=cfg.whisper_compute_type,
            whisper_model_instance=whisper_model_instance,
        )

    # Step 3 — Gemini AI analysis
    gemini_output_path = os.path.join(cfg.outputs_dir, "gemini_response.json")
    
    if getattr(cfg, "load_gemini_json", False) and os.path.exists(gemini_output_path):
        print(f"\n🔄 [3/3] Loading AI data ({cfg.ai_provider}) from local file: {gemini_output_path}")
        with open(gemini_output_path, "r", encoding="utf-8") as f:
            ai_clips = json.load(f)
    else:
        ai_clips = engine.analyze_with_ai(full_transcript, cfg)
        
        # Save raw gemini json for future loading/reproduction
        with open(gemini_output_path, "w", encoding="utf-8") as f:
            json.dump(ai_clips, f, indent=4, ensure_ascii=False)
        print(f"💾 Raw AI response saved to: {gemini_output_path}")

    # Step 4 — Metadata normalisation
    ai_clips = metadata.normalize_and_validate(ai_clips)
    if getattr(cfg, "snap_cuts", True):
        from . import clip_bounds

        clip_bounds.snap_clips(ai_clips, transcript_segments)
    metadata.print_preview(ai_clips)

    metadata_path = os.path.join(cfg.outputs_dir, "metadata_preview.json")
    metadata.save_metadata_preview(ai_clips, path=metadata_path)

    # Step 5 — Diarization (split-screen / camera-switch)
    diarization_data = None
    if (
        (getattr(cfg, "use_split_screen", False) and cfg.split_trigger == "diarization")
        or getattr(cfg, "use_camera_switch", False)
    ) and studio._is_vertical_ratio(cfg.aspect_ratio):
        try:
            mode_label = (
                "Split-Screen"
                if getattr(cfg, "use_split_screen", False)
                else "Camera-Switch"
            )
            print(f"\n🎙️ [{mode_label}] Running speaker diarization...")
            audio_path = cfg.original_video_file.replace(".mp4", "_audio.wav")
            diarization_mod.extract_audio(cfg.original_video_file, audio_path)
            num_speakers_arg = getattr(cfg, "diarization_num_speakers", 2)
            min_spk = None
            max_spk = None

            if str(num_speakers_arg).lower() == "auto":
                max_faces = studio.estimate_speaker_count_from_video(
                    cfg.original_video_file, cfg
                )
                num_speakers_arg = "auto"
                min_spk = max(1, max_faces)
                max_spk = min_spk + 2
                print(f"   ℹ️ Pyannote hint: {min_spk} to {max_spk} speakers.")

            diarization_data = diarization_mod.run_diarization(
                audio_path,
                hf_token=cfg.hf_token,
                num_speakers=num_speakers_arg,
                min_speakers=min_spk,
                max_speakers=max_spk,
            )
            # Clean up temp audio
            if os.path.exists(audio_path):
                os.remove(audio_path)
        except Exception as e:
            print(f"⚠️ Diarization failed: {e}")
            print("   Falling back to normal render mode (no split-screen).")
            diarization_data = None

    # Step 6 — Video encoder & glitch
    os.environ["OSC_VIDEO_SCALE_ALGO"] = str(
        getattr(cfg, "video_scale_algo", "bicubic")
    )
    
    # Get target dimensions for auto-bitrate calculation
    import cv2
    cap_e = cv2.VideoCapture(cfg.original_video_file)
    src_h_e = int(cap_e.get(cv2.CAP_PROP_FRAME_HEIGHT))
    cap_e.release()
    
    target_w_e, target_h_e = studio._get_render_dims(cfg, cfg.aspect_ratio, source_h=src_h_e)
    video_encoder = studio.detect_video_encoder(cfg, target_h=target_h_e)

    file_glitch_ts = None
    if cfg.use_hook_glitch:
        print("⚙️ Preparing glitch transition video...")
        
        # Get source dimensions for proper glitch scaling
        import cv2
        cap_g = cv2.VideoCapture(cfg.original_video_file)
        source_h_g = int(cap_g.get(cv2.CAP_PROP_FRAME_HEIGHT))
        cap_g.release()

        file_glitch_ts = studio.prepare_glitch_video(
            cfg.aspect_ratio, cfg, video_encoder, source_h=source_h_g
        )

    # Step 6 — Render each clip
    render_manifest: list[dict] = []

    custom_hook_path = None
    if getattr(cfg, "hook_source", None):
        print("\n🎣 Downloading custom Hook clip source...")
        custom_hook_path = hook_manager.download_custom_hook(cfg)

    # Step 5.5 — Generate Voice-Over (if enabled)
    if getattr(cfg, "voiceover", False):
        print(f"\n🎙️ Generating Voice-Over for {len(ai_clips)} clips...")
        for clip_item in ai_clips:
            try:
                # 1. Generate commentary script from snippet
                start = float(clip_item["start_time"])
                end = float(clip_item["end_time"])
                # Extract transcript snippet for this time range
                snippet_lines = []
                for seg in transcript_segments:
                    if float(seg["end"]) > start and float(seg["start"]) < end:
                        # Support both Whisper format (has 'text') and YouTube JSON3 (only 'words')
                        seg_text = seg.get("text") or " ".join(w["word"] for w in seg.get("words", []))
                        if seg_text:
                            snippet_lines.append(seg_text)
                snippet_text = " ".join(snippet_lines)

                script = voiceover.generate_commentary_script(
                    snippet_text,
                    cfg,
                    style=cfg.voiceover_style,
                    language=cfg.voiceover_lang,
                    length=cfg.voiceover_length,
                )

                if script:
                    # 2. Synthesize TTS
                    audio_path, vo_segments = voiceover.synthesize_voice(
                        script,
                        cfg.voiceover_voice,
                        cfg.outputs_dir,
                        str(clip_item["rank"])
                    )
                    
                    if os.path.exists(audio_path):
                        clip_item["voiceover"] = {
                            "script": script,
                            "audio_path": audio_path,
                            "segments": vo_segments,
                            "voice": cfg.voiceover_voice
                        }

            except Exception as e:
                print(f"   ⚠️ Failed to generate voice-over for Rank {clip_item['rank']}: {e}")

    for clip_item in sorted(ai_clips, key=lambda x: x["rank"]):
        
        if custom_hook_path:
            clip_item["custom_hook_info"] = {"file_path": custom_hook_path}

        render_result = studio.process_clip(
            clip_item["rank"],
            clip_item,
            cfg.aspect_ratio,
            file_glitch_ts,
            transcript_segments,
            cfg,
            video_encoder,
            diarization_data=diarization_data,
        )
        if render_result:
            render_manifest.append(render_result)

    # Step 7 — Inject source metadata for attribution & safety tracking
    for row in render_manifest:
        # Attach source URL so metadata.py can auto-add source credit
        if not row.get("source_url"):
            row["source_url"] = getattr(cfg, "url_youtube", None)

    # Step 7.5 — HyperFrames handoff packages (motion design happens later in Claude Code)
    if getattr(cfg, "handoff", False):
        from . import handoff

        handoff.export_all(render_manifest, ai_clips, transcript_segments, cfg)

    # Step 8 — Save manifest
    manifest_path = os.path.join(cfg.outputs_dir, "render_manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(render_manifest, f, ensure_ascii=False, indent=2)

    print(
        f"\n💾 Render manifest saved to {manifest_path} ({len(render_manifest)} item)"
    )


    return render_manifest

