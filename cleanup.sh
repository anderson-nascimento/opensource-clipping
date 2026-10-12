#!/bin/bash

echo "🧹 Starting temporary file cleanup..."

# Make sure the uploads and outputs directories exist
mkdir -p uploads outputs

# 1. Clean the uploads folder
echo "🗑️  Deleting all raw video files in uploads/..."
find uploads -type f -delete

# 2. Clean temporary files inside outputs/
echo "🗑️  Deleting raw source videos and temporary audio files in outputs/..."
# This keeps the final clips (rendered .mp4) and the JSON history
find outputs -type f -name "original_video.mp4" -delete
find outputs -type f -name "video_asli.mp4" -delete  # legacy name
find outputs -type f -name "*_audio.wav" -delete
find outputs -type f -name "*.json3" -delete

# 3. Clean Python caches
echo "🗑️  Deleting Python cache files (__pycache__)..."
find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null
find . -type d -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null
find . -type d -name ".ruff_cache" -exec rm -rf {} + 2>/dev/null

# 4. Clean frontend node_modules
echo "🗑️  Deleting frontend node_modules..."
# Docker uses its own volume for node_modules inside the container, so deleting it on the local host is safe.
rm -rf web/dashboard/node_modules
rm -rf web/dashboard/dist

# 5. Clean .cache & .local (WARNING)
# Careful: the .cache folder stores AI models (HuggingFace/Whisper) that are gigabytes in size!
# If deleted, the pipeline will re-download the AI models from scratch. Uncomment below ONLY if you really want to delete them.
# echo "🗑️  Deleting .cache and .local..."
# rm -rf .cache .local

echo "✅ Cleanup finished! Storage space has been freed."
