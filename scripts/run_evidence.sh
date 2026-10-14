#!/usr/bin/env bash
# Runs the full harness INSIDE the Linux test image and copies the raw outputs
# into evidence/. No bind mount: the container writes inside its own filesystem
# and `docker cp` exports the results, so host directory ownership and the
# container user's UID never matter. Nothing from the host (paths, user name,
# host name) ends up in the recorded files.
# Usage: scripts/run_evidence.sh
set -euo pipefail
cd "$(dirname "$0")/.."
docker build -q --target test -t m3sync-test . > /dev/null
docker build -q --target agent -t m3sync-agent . > /dev/null
cid=$(docker create --hostname harness m3sync-test \
  sh -c 'mkdir -p evidence/runs && python -m pytest -v -p no:cacheprovider --junitxml=evidence/junit.xml > evidence/pytest_output.txt 2>&1')
trap 'docker rm -f "$cid" > /dev/null 2>&1 || true' EXIT
set +e
docker start -a "$cid"
status=$?
set -e
mkdir -p evidence
find evidence/runs -name '*.log' -delete 2>/dev/null || true
docker cp "$cid:/app/evidence/." evidence/
# junit carries the container host name and timestamp attributes; keep neither.
sed -i.bak -E 's/ hostname="[^"]*"//; s/ timestamp="[^"]*"//' evidence/junit.xml && rm -f evidence/junit.xml.bak
{
  echo "tested code: src, sim, tests, Dockerfile of the working tree; uncommitted changes there: $(git status --porcelain -- src sim tests Dockerfile | wc -l | tr -d ' ')"
  echo "tree ids src:sim:tests (index): $(git write-tree --prefix=src/):$(git write-tree --prefix=sim/):$(git write-tree --prefix=tests/)"
  echo "date_utc: $(date -u +%Y-%m-%d)"
  echo "runtime: docker image m3sync-test (python:3.12.7-slim, linux), run as uid 10001"
  echo "simulator sha256: $(shasum -a 256 sim/m3sim.py | cut -d' ' -f1)"
  echo "command: python -m pytest -v -p no:cacheprovider --junitxml=evidence/junit.xml"
  echo "result: $(tail -n 1 evidence/pytest_output.txt | tr -d '=' | sed 's/^ *//; s/ *$//')"
  echo "agent image ExposedPorts: $(docker inspect -f '{{json .Config.ExposedPorts}}' m3sync-agent)"
  echo "agent image Healthcheck: $(docker inspect -f '{{json .Config.Healthcheck.Test}}' m3sync-agent)"
} > evidence/RUN_INFO.txt
cat evidence/RUN_INFO.txt
exit $status
