# GitHub + PandaStack disposable runtime deployment

## GitHub

Create two repositories:

- `panda-board`: source code.
- `panda-board-data`: durable structured data and the `site/` directory.

Enable GitHub Pages for `panda-board-data` from the `main` branch and `/site` directory.

The data repository must contain only public board data. Never commit tokens.

## PandaStack Function

Create one private Python Function and configure:

- `PANDASTACK_API_KEY`
- `PANDASTACK_API_URL=https://api.pandastack.ai`
- `PANDA_BOARD_CODE_REPO=OWNER/panda-board`
- `PANDA_BOARD_CODE_BRANCH=main`
- `BOARD_DATA_REPO=OWNER/panda-board-data`
- `BOARD_DATA_BRANCH=main`
- `GITHUB_TOKEN=<token with write access to panda-board-data and read access to panda-board>`
- `PANDASTACK_SANDBOX_TEMPLATE=code-interpreter`
- `PANDASTACK_SANDBOX_TTL=1800`
- `PANDASTACK_JOB_TIMEOUT=300`

Deploy `function/handler.py` together with `app/panda_api.py`.

## Schedule

Create a Schedule that invokes this Function once per day, for example at 18:30 Asia/Shanghai. The Schedule itself only triggers the Function; it does not keep a Sandbox alive.

## Runtime

The Function creates one sandbox with a finite TTL, writes the GitHub token to the sandbox's ephemeral filesystem, clones the source repository, installs requirements, runs `python3 -m app.cli remote-cycle`, and always requests sandbox deletion in `finally`.

The command performs:

1. clone/update data repository;
2. import durable GitHub data into temporary SQLite;
3. collect announcements, K-lines and market value;
4. reject partial/failed collection before publication;
5. build static site into `data-repo/site`;
6. export structured data into `data-repo/data`;
7. commit and push one atomic Git commit to `main`;
8. exit; Function deletes the Sandbox.

If collection/build/push fails, the data repository is not modified by that run. The Sandbox TTL is a second cleanup mechanism if explicit deletion is not confirmed.
