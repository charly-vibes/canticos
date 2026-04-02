# canticos

Cánticos, invocaciones, escrituras — scripts de uso personal.

## Scripts

| Script | Descripción |
|---|---|
| `claude-dashboard` | Dashboard HTML de uso de Claude Code |
| `projector-queue` | Procesa una cola de videos a través de `to-projector` |
| `to-projector` | Convierte video a MP4 compatible con proyector (H.264 + AAC estéreo) |
| `transcribe` | Transcribe audio a texto con whisper-cli |

## Instalación

```sh
./install
```

Crea symlinks de `bin/*` en `~/.local/bin`. Si ya existe un archivo con el mismo nombre, lo respalda como `.bak`.

Para instalar en otro directorio:

```sh
./install /otro/path
```
