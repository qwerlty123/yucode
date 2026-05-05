ARG BASE_IMAGE
FROM ${BASE_IMAGE}

USER root
COPY --from=ghcr.io/astral-sh/uv:0.8.15 /uv /uvx /bin/
ENV UV_PYTHON_INSTALL_DIR=/opt/uv-python \
    UV_LINK_MODE=copy \
    PYTHONPATH=/opt/yucode \
    PATH=/opt/yucode-venv/bin:$PATH
WORKDIR /opt/yucode
COPY pyproject.toml README.md ./
COPY yucode ./yucode
COPY evals ./evals
RUN uv python install 3.11 \
    && uv venv --python 3.11 /opt/yucode-venv \
    && uv pip install --python /opt/yucode-venv/bin/python /opt/yucode

WORKDIR /workspace
