# The base image by content, not by name. A tag is a label upstream can move, and the layer
# below installs this project's dependencies with whatever `pip` and whatever CA bundle this
# image happens to carry, so pinning the digest is what fixes those too. The tag is kept
# beside it so a reader can see which release this is, and so Dependabot's docker ecosystem
# has a tag to follow when it moves the digest on.
#
# This is the OCI *index* digest rather than one platform's manifest, so the build still
# resolves on arm64 as well as amd64.
FROM python:3.14-slim@sha256:51dafde81dbdb6ebde285137a295cf18a47ca95234fe388a343719cb97305b3d

WORKDIR /app

COPY requirements.txt .
# `--require-hashes` is the point of the file, not a flourish: without it pip would accept
# whatever the index served for each pinned version, and the whole set is pinned only so that
# every artefact is checked against a hash somebody reviewed. It also refuses the file
# outright if any requirement in it has lost its hash, so a hand-edited lock fails the build
# rather than quietly widening it.
RUN pip install --no-cache-dir --require-hashes -r requirements.txt

COPY app ./app

ENV PYTHONUNBUFFERED=1 \
    CLAUDE_DATA_DIR=/data/claude \
    CODEX_DATA_DIR=/data/codex

EXPOSE 8000

# The bound on a request head and on connections lives here, in the process that holds both
# tokens and does the parsing, rather than only in the `ingress` relay (#43): `app/server.py`
# names the values and `uvicorn`'s own defaults arm none of them.
CMD ["python", "-m", "app.server", "--bind", "0.0.0.0", "--port", "8000"]
