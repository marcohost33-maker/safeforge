# Runs of Vero's `diskriminierung.py` (private tool, not in this repo)

Byte-exact as written by the tool; the spec is the file the runs hashed
(`spec_sha256`), including the absolute paths of the cloud container they
ran in. The JSONL lines are **pending append** to the canonical ledger
`G:\Meine Ablage\Vero\Data\diskriminierung_runs.jsonl`, which the cloud
container cannot reach (`VERO_DISKRIMINIERUNG_LEDGER` pointed elsewhere, so
the tool flagged the ledger as "NICHT der kanonische Ledger").

| run_id | head | verdict | provenance |
|---|---|---|---|
| DK-20261021T115042-5379225cb457 | 9a8c7fe | 28/31 (3 red only by crash) | VOLL |
| DK-20261021T115849-4b810d953a1a | 8be4ed5 | **31/31 DISKRIMINIERT** | VOLL |

Tool: `diskriminierung.py`, Drive id `1tb0y2MgRKRCdV-H5vNA-IBdDm7lrBPkv`,
136891 bytes, sha256 `59a732576ceaa7855a05eb2ab901f81758ad2e44f2a2981bf71fed0aed3df35c`.
