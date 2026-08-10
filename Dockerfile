# TenderMining — one image, one cycle. doc/STORAGE.md 6.5.
#
#   docker compose build
#   docker compose run --rm tm python loop.py run --last 7d
#
# The claim this file has to earn: a week's reports come out with nothing from
# the operator's laptop involved except the state directory that is mounted in.
# Code lives in the image and is read-only; every path the cycle writes to is
# named by TM_DATA_DIR or TM_MODELS_DIR and therefore outlives the container.

FROM python:3.13-slim-bookworm

# libgomp1: CatBoost's wheel links against OpenMP and fails at import without
# it — the one system library the slim image does not already carry.
# tzdata: report filenames are dates, so the container has to agree with the
# operator about which day it is. See TZ below.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libgomp1 tzdata \
 && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies before code: the 900 MB wheel layer is cached and re-used for
# every code change, so an edit to loop.py rebuilds in seconds.
COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY . .

# Where this deployment's state lives (doc/STORAGE.md 6.1 and 6.5). Both are
# mount points, and both are outside /app on purpose: if a cycle ever writes
# into the code directory again, it shows up as a change to a container's
# ephemeral layer instead of quietly persisting on a laptop.
#
# /models is the trained-model registry (registry.jsonl, CURRENT, binaries).
# It defaults under /data so a single mounted volume carries the whole state,
# which is what makes "swap the laptop for a host" one decision rather than two.
ENV TM_DATA_DIR=/data \
    TM_MODELS_DIR=/data/models

# The embedding model is 309 MB fetched from HuggingFace on first use. Cached
# in its own volume rather than baked into a layer: the image stays small and
# rebuildable offline-free, and the download happens once per host, not once
# per image build.
ENV FASTEMBED_CACHE_PATH=/models_cache \
    HF_HOME=/models_cache/huggingface

# German notices, German deadlines, and report_<date>.html names the operator's
# day — a UTC container would file Monday 00:30 CEST under Sunday. Override at
# run time (compose passes TM_TZ) if the deployment sits elsewhere.
ENV TZ=Europe/Berlin

# Non-root, and the mount points exist and are owned before the volumes are
# created: Docker seeds a named volume's ownership from the image, so getting
# this wrong shows up as a permission error three minutes into a cycle.
RUN useradd --create-home --uid 1000 tm \
 && mkdir -p /data /models_cache \
 && chown tm:tm /data /models_cache
USER tm

# One cycle, the RUNBOOK's routine command. Override freely:
#   docker compose run --rm tm python loop.py run --last 2d --skip-download
CMD ["python", "loop.py", "run", "--last", "7d"]
