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
# tzdata: several programs date their work with date.today(), so the container
# has to agree with the operator about which day it is. See TZ below.
# cron: only the `scheduler` service runs it (docker-compose.yml), but it lives
# in the same image so the thing that fires the cycle and the thing that runs it
# can never be two different builds.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libgomp1 tzdata cron \
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

# The operator's laptop clock, which is what the cycle has always been dated by.
# loop.py's own report names come from now_utc() and do not care — but bulk.py
# and download.py pick the download window with date.today(), backtest.py and
# calibrate.py name their receipts the same way, and the weekly schedule fires
# at 08:15 local. A UTC container would shift all of those by two or three hours
# and, once a day, by a whole date. Override at run time (compose passes TM_TZ).
ENV TZ=Europe/Bucharest

# Non-root, and the mount points exist and are owned before the volumes are
# created: Docker seeds a named volume's ownership from the image, so getting
# this wrong shows up as a permission error three minutes into a cycle.
RUN useradd --create-home --uid 1000 tm \
 && mkdir -p /data /models_cache \
 && chown tm:tm /data /models_cache

# The weekly schedule. cron refuses a cron.d file that is group/world-writable
# or executable, and does so silently — which is the failure mode where the
# container looks healthy for a week and then no report arrives on Monday.
# Copying via /app keeps the file under version control as docker/crontab.
# The exec bit is set here rather than relied on from the build context: the
# checkout is on Windows, which does not carry one.
RUN install -m 0644 -o root -g root /app/docker/crontab /etc/cron.d/tendermining \
 && chmod 0755 /app/docker/weekly.sh

USER tm

# One cycle, the RUNBOOK's routine command. Override freely:
#   docker compose run --rm tm python loop.py run --last 2d --skip-download
CMD ["python", "loop.py", "run", "--last", "7d"]
