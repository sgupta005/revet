GITHUB_API = "https://api.github.com"
INST_TOKEN_KEY = "gh:inst_token:{installation_id}"
INST_TOKEN_TTL_BUFFER = 60  # refresh before GitHub's stated expiry to avoid edge-of-expiry use

DEDUP_KEY = "gh:delivery:{delivery_id}"
DEDUP_TTL = 24 * 3600
