# Explicit synthetic DEVELOPMENT/TEST sandbox fixture. No dependency downloads.
FROM node@sha256:55fb5f5a527ea2c5c4a95f963e48ef90aa38c7b346054fdc8ee9957a2b55d205
COPY --chmod=0555 server.mjs /fixture/server.mjs
LABEL archer.fixture="development-mcp-sandbox-only"
ENTRYPOINT ["node", "/fixture/server.mjs"]
