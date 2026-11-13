# canticos

Cánticos, invocaciones, escrituras — scripts de uso personal.

## Apps

| App | Descripción |
|---|---|
| [`anatomico`](apps/anatomico/) | *Quiero un zoom anatómico* — clon de ZoomIt para GNOME Shell, nativo en Wayland |

## Scripts

| Script | Descripción |
|---|---|
| `llm-dashboard` | Dashboard HTML local de uso de asistentes (Claude Code + Pi) y actividad de Amp/Gemini |
| `claude-dashboard` | Alias retrocompatible de `llm-dashboard` |
| `projector-queue` | Procesa la cola `~/Downloads/MOVIES/` a través de `to-projector`; clasifica cada entrada como serie o película y la envía a `~/PROJECTOR/Shows/` o `~/PROJECTOR/Movies/` |
| `to-projector` | Convierte video a MKV compatible con proyector (H.264 + AAC estéreo); ordena episodios en estructura Jellyfin `Show (Year)/Season NN/` bajo el directorio de salida |
| `transcribe` | Transcribe audio a texto con whisper-cli |
| `meeting-rec` | Graba audio de reuniones (micrófono + salida del sistema en pistas Opus separadas) en un `.mka` hasta Ctrl+C; detección automática de dispositivos via PipeWire |
| `interview-live` | Copiloto de entrevistas Mom Test con dashboard reactivo (curses): al iniciar marca cuántos participantes hay, muestra el transcript en vivo y una cola priorizada de preguntas + temas importantes (los recurrentes suben solos). Transcribe micrófono + sistema en vivo (diarizado, L/0=entrevistador, R/1=invitado) y sugiere preguntas vía pi contra el plan individual qwen. Las sesiones viven en `<out>/<session>/` (`--list`/`--adopt`), hay replay (`--from`/`--replay`) y las sugerencias se disparan al terminar el turno del invitado, no por ronda — ver [interview-live](#interview-live) |
| `terapia` | Consulta CLI en nushell sobre `~/.pi/agent/sessions`: sesiones, tool calls y errores clasificados como JSON — la alternativa estable al heredoc-parser de turno |
| `fi` | Wrapper de modelos libres (`:free`) para `pi`; alias simples (`laguna`, `nemo2`, `openrouter`) y paso directo; `doctor` / `list` / `--timeout` |

## Instalación

```sh
./install
```

Crea symlinks de `bin/*` en `~/.local/bin`. Si ya existe un archivo con el mismo nombre, lo respalda como `.bak`.

Para instalar en otro directorio:

```sh
./install /otro/path
```

### interview-live

Copiloto de entrevistas Mom Test. Transcribe en vivo micrófono + audio del sistema (diarizado: speaker 0 = entrevistador, speaker 1 = invitado), mantiene el transcript y una cola priorizada de preguntas + temas, y consulta a pi contra el plan individual qwen. Las sugerencias llegan cuando termina el turno del invitado, no en cada ronda del timer.

`--out` es la **raíz** de sesiones: cada corrida escribe en `<out>/<session>/` (`transcript.txt`, `questions.md`, `queue.json`, `speakers.json`, `events.jsonl`, `capture.wav`). La raíz por defecto es `$INTERVIEW_DIR` o `~/Interviews`.

```sh
interview-live                                               # sesión nueva interview-YYYYMMDD-HHMMSS
interview-live --out ~/Interviews --session ana-noviembre
interview-live --list                                        # sesiones, más nuevas primero
interview-live --list --json                                 # {"sessions": [...]} para scripts
interview-live --adopt                                       # mueve artefactos planos legacy a adopt-<ts>/
interview-live --session ana-noviembre --speaker 0=Vos --speaker 1=Ana
interview-live --session ana-noviembre --set-speaker 1=Ana   # reetiqueta sin retranscribir
interview-live --replay ~/grabaciones/ana.wav --window 10 --min-segments 4  # gold harness
interview-live --session ana-noviembre --diarize             # pyannote N-speakers + HF_TOKEN
```

| Flag | Para qué |
|---|---|
| `--out DIR` | Raíz de sesiones; cada sesión vive en `<out>/<session>/` (default `$INTERVIEW_DIR` o `~/Interviews`) |
| `--session NAME` | Nombre de la sesión bajo `--out`; reutilizarlo **agrega** a esa sesión (default `interview-YYYYMMDD-HHMMSS`) |
| `--list` / `--list --json` | Lista sesiones, más nuevas primero (nombre, fecha, duración, grabación, líneas, preguntas); `--json` emite `{"sessions": [...]}` y solo vale con `--list` |
| `--adopt` | Migración única: mueve `capture.wav`, `transcript.txt`, … y `window-*.wav` planos de la raíz a `adopt-<ts>/` para que `--list` los muestre; no-op si no hay nada que mover |
| `--speaker N=Name` | Nombra un speaker diarizado en vivo (repetible); persiste en `speakers.json`, renderiza `[ts] Nombre: texto` y completa el id 0 como `You` si no se nombra |
| `--set-speaker N=Name` | Reetiqueta post-hoc una sesión existente (requiere `--session`); no retranscribe |
| `--replay FILE` | Gold harness: reproduce una grabación por la ventana móvil como si fuera en vivo (sin micrófono) para comparar transcript y sugerencias contra una conversación de referencia; ejemplo `--window 10 --min-segments 4` |
| `--from FILE` | Replay de una sola pasada (una transcripción completa, sin streaming) |
| `--diarize` | Re-diariza `capture.wav` con pyannote (N speakers) y reasigna líneas por solapamiento temporal; requiere `--session` y `HF_TOKEN`, corre en un entorno efímero de `uv` (el primer run baja torch, ~2 GB) y no se combina con `--list`/`--adopt`/`--set-speaker` |
| `--participants N` | Cantidad de participantes (default 2); alimenta el roster de la TUI y el system prompt de pi |
| `--no-tui` / `--tui` | Fuerza salida plana o el dashboard curses (default: automático según TTY) |

Defaults pensados para entrevistas en español: `--language es` (el `auto` por ventana se equivoca con clips cortos), `--every 5` segundos entre pasadas de whisper y `--min-segments 1`. Whisper usa `$WHISPER_MODEL` si está seteado; si no, prefiere `~/models/ggml-large-v3-turbo.bin` y cae a `ggml-base.bin`. El modelo de pi es `qwen-token-plan-individual/qwen3.8-flash` (override `--pi-model` / `$INTERVIEW_PI_MODEL`).

**Cuándo sugiere.** Los segmentos se acumulan y el batch se dispara al terminar el turno del invitado (piso de silencio de 1,2 s), al superar 25 s de monólogo, con la tecla `f` en la TUI (pedir ahora) o en el flush de fin de corrida; nunca una vez por ronda. Si llegan batches más nuevos mientras pi está ocupado, los viejos se descartan pero sus líneas se suman al batch nuevo (coalescen: no se pierde ningún segmento del transcript), dejando `suggest_trigger` / `suggest_stale_drop` en `events.jsonl`.

Cifras de referencia medidas sobre la sesión `~/Interviews/interview-20261113-084517` (83,5 min, 222 batches, 628 preguntas, cadencia mediana 21 s); no se copian de las propuestas externas.

### terapia

Salida JSON tipada sobre el corpus de sesiones de Pi; implementa una de las recomendaciones del reporte `../pain/report.org` (contratos de salida estructurados, kill-the-heredoc).

```sh
terapia list --project areas/dev/gh/charly --since 2026-10-01  # sesiones como JSON
terapia stats --all --since 2026-10-09                        # conteos + errores por clase
terapia errors --all --group-by class                         # taxonomía de errores
terapia tools <session-id> --tool bash --errors-only          # tool calls fallidos
terapia raw <session-id> --type session                       # JSONL crudo
```

Tests: `python3 tests/test_terapia.py` (fixture sintético; no requiere el corpus real).


---

## A note on authorship

All the code in this repository was generated by a large language model. This is not a confession, nor an apology. It's a fact, like the one that says water boils at a hundred degrees at sea level: neutral, technical, and with consequences one discovers later.

What the human did is what tends to happen before and after things come into existence: thinking. Reviewing requirements, arguing about edge cases, understanding what needs to be built and why, deciding how the system should behave when reality —which is capricious and does not read documentation— confronts it with situations nobody anticipated. The hours of planning, of design, of reading specifications until exhaustion dissolves the boundary between understanding and hallucination.

The LLM writes. The human knows what it should say.

There is a distinction, even if looking at the commit history makes it hard to find. The distinction is that a machine can produce correct code without understanding anything, the same way a calculator can solve an integral without knowing what time is. Understanding what that integral is *for*, whether it actually solves the problem, whether the problem was the right problem to begin with — that remains human territory. For now.

*[Leer en español](https://charly-vibes.github.io/charly-vibes/)*
