GITHUB_API = "https://api.github.com"
INST_TOKEN_KEY = "gh:inst_token:{installation_id}"
INST_TOKEN_TTL_BUFFER = 60  # refresh before GitHub's stated expiry to avoid edge-of-expiry use

DEDUP_KEY = "gh:delivery:{delivery_id}"
DEDUP_TTL = 24 * 3600

# Phase 5 — user-to-server OAuth (the authorize/code-exchange host, distinct from the API host).
GITHUB_OAUTH_BASE = "https://github.com"

# Phase 5 — Redis session + access-check caches.
SESSION_KEY = "auth:session:{session_token}"
USER_INSTALLATIONS_KEY = "auth:user_installs:{user_id}"
USER_REPOS_KEY = "auth:user_repos:{user_id}:{installation_id}"
USER_CACHE_TTL = 300  # short TTL: identity/access lists refresh frequently

# PR review
PER_PAGE = 100
MAX_FILE_PAGES = 3  # cap the PR size we review (≤ 300 changed files)
