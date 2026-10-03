# Package layout

What each file in the repository is for. [`CLAUDE.md`](../CLAUDE.md) is the map of how the code
fits together, and is written for an agent working here but reads as well for a person.

```
.
├── app/
│   ├── main.py          # FastAPI app + SSE stream + the payload boundary
│   ├── degrade.py       # The fixed vocabulary the boundary reports failures with
│   ├── quota.py         # Claude live client → claude.ai/api/oauth/usage
│   ├── activity_gate.py # The one gate both activity readers reach the filesystem through
│   ├── claude_activity.py # Claude local activity timestamp reader
│   ├── codex_quota.py   # Codex live client → chatgpt.com/backend-api/wham/usage
│   ├── codex_activity.py # Codex local activity metadata reader
│   ├── refresh.py       # One background refresher per source: when a source is read
│   ├── budget.py        # What a single payload-feeding read may cost
│   ├── server.py        # The server the image launches, and its request/connection bound
│   ├── egress.py        # Allow-listing CONNECT proxy: the dashboard's only route out
│   ├── ingress.py       # Relay that publishes the dashboard's port
│   ├── templates/
│   │   └── index.html
│   └── static/
│       ├── style.css
│       ├── widget-state.js # Browser-local persistence and presentation state
│       └── app.js          # Gauge rendering, DOM updates, and SSE handling
├── Dockerfile
├── docker-compose.yml
├── requirements.in       # The packages the app needs, by name
├── requirements.txt      # Those resolved in full and fixed by content hash
├── requirements-dev.in   # The above plus what the tests and the linter need
├── requirements-dev.txt  # Those resolved in full and fixed by content hash
├── requirements-screenshots.in  # The runtime set plus playwright/pillow, by name
├── requirements-screenshots.txt # Those resolved in full and fixed by content hash
├── pytest.ini
├── tests/
├── tools/screenshots/   # Renders the README's image and the social preview from fabricated data
├── docs/
│   ├── operations.md      # Configuration, serving other machines, the egress check, troubleshooting
│   ├── security-model.md  # What bounds the container and the dashboard, and what an engine leaves open
│   ├── package-layout.md  # This file
│   └── images/            # The README's image and the social preview
├── .env.example
├── .gitignore
├── CONTRIBUTING.md
├── SECURITY.md
└── LICENSE
```
