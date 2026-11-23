# Changelog

All notable changes to bunch are recorded here. The project follows a single
rolling `main` branch; each entry links to the pull request that made it.

## Unreleased

- One shared jobs API in the hub decides what an agent should answer (already answered, double send, loop guard, turn order, budget, offering again after a crash) for workers, hub-run agents and `format=json` readers; `clients/responder.py` now gets turn-taking, the double-send check and a retry after a crash. `RESPONDER_REPLY_TO_AGENTS` is removed (and the responder no longer guesses for older hubs) because the hub now decides who answers (#154).

- The echo filter for hub-run agents also catches replies that address the agent itself, continue the room summary, copy recent messages or write for other speakers; such a reply is retried once, then the person gets a short note (#124).

- Hub-run agents can save files to a room's folder with a `save: path` line above a code block, and no longer claim to upload when the room has no folder (#123).
