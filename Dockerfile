FROM dailyco/pipecat-base:latest

# Enable bytecode compilation
ENV UV_COMPILE_BYTECODE=1

# Copy from the cache instead of linking since it's a mounted volume
ENV UV_LINK_MODE=copy

# Install dependencies (no project install — keeps layer cached across src
# edits). uv.lock + pyproject are bind-mounted so they don't bloat the image.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project --no-dev

# Application code. The previous Dockerfile copied only bot.py, which meant
# `from prosper.bot import bot` failed at runtime — the `src/prosper`
# package was never inside the image. Copy the full source layout plus
# pyproject so the editable install resolves the `prosper` package.
COPY ./pyproject.toml ./pyproject.toml
COPY ./uv.lock ./uv.lock
COPY ./src ./src
COPY ./scripts ./scripts
COPY ./bot.py ./bot.py

# Install the project itself (now that src/ is present) so the `prosper`
# package is importable. --no-deps because deps are already synced above.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --no-deps -e .
