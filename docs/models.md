# Adding models and Ollama servers

This guide covers how bunch finds out about models, and what to do when you pull a new model or set up a new Ollama server.

## How bunch finds models

**bunch never talks to Ollama directly, and it doesn't discover models on its own.** Every model goes through the gateway (LiteLLM), and the gateway only knows about the models listed in `litellm/config.yaml`.

```
  litellm/config.yaml  ──read at start-up──►  gateway :4000  ──/v1/models──►  bunch (model pickers)
  (your list of models)                        │
                                               ├──► Ollama on the GPU box
                                               ├──► Ollama on the mini PC
                                               └──► paid APIs
```

That means:

- Pulling a model with `ollama pull` on some machine does nothing for bunch until you add it to `config.yaml`.
- Starting Ollama on a new machine does nothing for bunch until you add that machine's address to `.env` and its models to `config.yaml`.
- Pairing a computer as a bunch **machine** (`clients/worker.py`) doesn't add its Ollama models either. The worker reports the CLIs on that computer (Claude Code, Codex, Antigravity) and the names and IPs it's reachable at. bunch uses those names and IPs only to group gateway models that are *already in the config* under that computer in the viewer (see [Showing a model under its computer](#showing-a-model-under-its-computer)).
- You can't add models from the web viewer or through the MCP tools. The viewer only reads the gateway's list so you can pick a model for an agent. The MCP tools (`read`, `post`, `get`, `ask`, `rooms`) are for chatting.

Once a model is in the config and the gateway has been restarted, the rest happens by itself: it shows up in the viewer's "Who answers?" picker, and agents, `ask`, summaries, the router and `responder.py` can all use it by name.

## The two files

| File | What goes in it | In git? |
|---|---|---|
| `.env` | Where each Ollama server lives (`OLLAMA_..._URL`), plus any paid API keys | No (git-ignored) |
| `litellm/config.yaml` | One entry per model: the name clients use, the real model, and which server it's on | No (git-ignored; copy it from `litellm/config.example.yaml`) |

`config.yaml` holds no addresses or secrets itself. It refers to `.env` values with `os.environ/NAME`, so you can share the model list without sharing your network layout.

A model entry looks like this:

```yaml
  - model_name: local/coder                      # the name clients and bunch ask for
    litellm_params:
      model: ollama_chat/qwen2.5-coder:14b       # ollama_chat/ + the exact tag from `ollama list`
      api_base: os.environ/OLLAMA_GPU_URL        # which Ollama server, from .env
```

- **`model_name`** is up to you. Describe the job or the model, not the machine (`local/coder`, not `gpu-box-qwen`). Then you can move a model to another computer later without changing any agent, script or setting that uses the name.
- **`model`** must be `ollama_chat/` followed by the model's tag exactly as `ollama list` prints it on that server, for example `llama3.1:8b`. A typo or a model that isn't pulled on that server makes requests fail; the gateway doesn't pull models for you.
- **`api_base`** points at the server. Always go through `os.environ/...` rather than writing the address here.

## Adding a model to an Ollama server bunch already uses

1. On the Ollama machine, pull the model and note its exact tag:

   ```bash
   ollama pull mistral-small:24b
   ollama list
   ```

2. On the bunch server, add an entry to `litellm/config.yaml`, reusing that server's existing variable:

   ```yaml
     - model_name: local/writer
       litellm_params:
         model: ollama_chat/mistral-small:24b
         api_base: os.environ/OLLAMA_GPU_URL
   ```

3. Restart the gateway so it reads the file again:

   ```bash
   docker compose restart litellm
   ```

4. Check it, then [use it](#using-the-new-model-in-bunch).

## Adding a new Ollama server

### 1. Make Ollama reachable from the bunch server

By default Ollama only listens on its own machine. On the new machine, have it listen on the network and allow only the bunch server through the firewall. Ollama has no passwords, so anyone who can reach port 11434 can use it. The steps are in [proxmox.md](proxmox.md#making-ollama-reachable-from-bunch).

Then check it from the bunch server:

```bash
curl http://<new-machine>:11434/api/tags
```

You should get a JSON list of the models pulled on that machine. Use those names in step 3.

### 2. Add its address to `.env`

Pick a variable name for it. Any name works as long as `config.yaml` uses the same one:

```bash
OLLAMA_STUDIO_URL=http://studio.lan:11434
```

Prefer a hostname or a fixed address (a DHCP reservation on your router). If the machine's IP changes later, you only have to update this one line.

### 3. Add its models to `litellm/config.yaml`

One entry per model, each pointing at the new variable.

### 4. Recreate the gateway

```bash
docker compose up -d litellm
```

Use `up -d` here, not `restart`. Docker only reads `.env` when it creates a container. `restart` restarts the same container with its old environment, so the gateway wouldn't see the new `OLLAMA_..._URL`. `up -d` notices that `.env` changed and recreates the container. After a change to `config.yaml` alone, `restart` is enough.

## Checking it worked

List what the gateway now serves:

```bash
curl http://<server>:4000/v1/models -H "Authorization: Bearer $LITELLM_MASTER_KEY"
```

Then send it one real message, which proves the gateway can reach the Ollama server and the model tag is right:

```bash
curl http://<server>:4000/v1/chat/completions \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" -H "Content-Type: application/json" \
  -d '{"model": "local/writer", "messages": [{"role": "user", "content": "Say hi in five words."}]}'
```

The first request can take a while, because Ollama has to load the model into memory. If it fails, `docker compose logs --tail 50 litellm` usually says why. The common causes are a typo in the tag, a model that isn't pulled on that server, a wrong address in `.env`, or a firewall blocking port 11434.

## Using the new model in bunch

- **As an agent:** in the viewer, make an agent (or open an existing one) and pick the model under "Who answers?". The hub sends that agent's messages through the gateway to the model, and nothing has to run on the Ollama machine besides Ollama. The viewer fetches the model list once per page load, so reload the page if a model you just added isn't in the picker. From the command line:

  ```bash
  docker compose exec bunch python -m bunch.cli add-agent writer --model local/writer \
    --desc "drafts and rewrites text" --system "You are writer. Be brief."
  ```

- **For summaries or routing:** set `BUNCH_SUMMARY_MODEL` or `BUNCH_ROUTER_MODEL` in `.env` to the model's name, then run `docker compose up -d bunch`. Use a small, fast model for these, since they run often.
- **With the responder:** `BUNCH_KEY=bunch_xxx RESPONDER_MODEL=local/writer python3 clients/responder.py` (see [WALKTHROUGH.md](WALKTHROUGH.md#8-making-a-local-model-join-the-chat-the-responder)).
- **From any other app:** point any OpenAI-compatible tool at `http://<server>:4000/v1` with the gateway key and ask for the model by name.

## Showing a model under its computer

The viewer groups agents and models by the computer that does the work. A gateway model is listed under a paired machine when the host in its `api_base` matches that machine. bunch reads each model's `api_base` from the gateway and compares its host with each paired machine's:

- name in bunch,
- hostname,
- addresses reported by its worker (LAN IP, Tailscale IPs and Tailscale name).

For a hostname, the part before the first dot counts too, so `http://studio.lan:11434` matches a machine named `studio`. Models that don't match a paired machine, and paid APIs, are listed under "Other servers & cloud APIs".

Once a model is grouped under a machine, bunch knows that agents on it only work while that machine is on. When the machine is off, they reply "offline" straight away.

bunch refreshes this mapping at most every 5 minutes, so a newly added model can show up under "Other servers" for a few minutes before it moves under its machine.

## Handy extras

- **Same name on two servers** spreads requests between them:

  ```yaml
    - model_name: local/general
      litellm_params: { model: ollama_chat/llama3.1:8b, api_base: os.environ/OLLAMA_GPU_URL }
    - model_name: local/general
      litellm_params: { model: ollama_chat/llama3.1:8b, api_base: os.environ/OLLAMA_STUDIO_URL }
  ```

- **Fallbacks** use another model when one is down or busy, for example when the GPU box is switched off:

  ```yaml
  router_settings:
    fallbacks:
      - local/general: ["local/small"]
  ```

- **Thinking models used for summaries:** bunch keeps summaries very short (about 160 tokens), and a model that "thinks" first can use them all up before it writes anything. For Ollama models that support it, add `think: false` under that entry's `litellm_params`.
- **Moving a model** to another computer is a one-line change to its `api_base` plus a gateway restart. Agents keep working, because they use the `model_name`.
- **Removing a model:** delete its entry and restart the gateway. Agents still set to that model will fail when asked, so switch them to another model in the viewer first.

## Worked example

A home has the bunch server, a GPU box that's already set up, and a new machine, `studio`, that just got Ollama. The owner wants a writing agent on it.

On `studio`:

```bash
ollama pull mistral-small:24b
ollama pull qwen2.5:3b
sudo systemctl edit ollama          # add: Environment="OLLAMA_HOST=0.0.0.0:11434"
sudo systemctl restart ollama
sudo ufw allow from 192.0.2.10 to any port 11434    # 192.0.2.10 = the bunch server
```

On the bunch server, first check `studio` is reachable:

```bash
curl http://studio.lan:11434/api/tags
```

Add a line to `.env`:

```bash
OLLAMA_STUDIO_URL=http://studio.lan:11434
```

Add two entries to `litellm/config.yaml`:

```yaml
  - model_name: local/writer
    litellm_params:
      model: ollama_chat/mistral-small:24b
      api_base: os.environ/OLLAMA_STUDIO_URL

  - model_name: local/small
    litellm_params:
      model: ollama_chat/qwen2.5:3b
      api_base: os.environ/OLLAMA_STUDIO_URL
```

Recreate the gateway, since `.env` changed, and check both models:

```bash
docker compose up -d litellm
curl http://192.0.2.10:4000/v1/models -H "Authorization: Bearer $LITELLM_MASTER_KEY"
```

Create the agent:

```bash
docker compose exec bunch python -m bunch.cli add-agent writer --model local/writer \
  --desc "drafts and rewrites text"
```

Now `@writer tighten up this paragraph: ...` in any room gets an answer from `studio`. If `studio` is also paired as a machine named `studio`, the viewer lists `local/writer` and `local/small` under it, and `writer` replies "offline" while `studio` is switched off.
