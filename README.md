# KOF 2002 (PS2) — Pipeline de ROM Hacking & Custom Character (Rugal / SLES-53381)

Pipeline completo em Python para extração, decodificação de sprites, edição no LibreSprite/Aseprite, reinjeção LBA-safe em imagem Mode 2 / 2352 (`.bin` + `.cue`) e substituição de texturas no **PCSX2 Qt (v2.8.2)** para **The King of Fighters 2002 (PS2 — Europa, `SLES_533.81` / `SLES-53381`)**.

> **Segurança:** Nenhum script deste projeto altera a ISO/BIN original ou os arquivos dentro de `extracted/`. Todo patch é gravado em cópias dentro de `build/`.

---

## Estrutura de Pastas

```text
G:\KOF\
├── .venv/                         # Ambiente virtual Python (ignorado no Git)
├── .gitignore                     # Ignora discos, extrações, builds e dumps pesados
├── King of Fighters 2002, The (Europe).bin / .cue  # Imagem original (somente leitura)
├── extracted/                     # Arquivos extraídos da imagem PS2 (ignorado no Git)
├── sprites_dump/                  # Sprites decodificados, sheets e offsets_map.json (ignorado no Git)
├── custom_character/              # Seus frames editados em PNG Indexado 16 cores (versionado no Git)
├── build/                         # Arquivos .BIN/.GRC patcheados e kof2002_mod.bin/.cue (ignorado no Git)
├── textures/
│   └── SLES-53381/
│       ├── dumps/                 # Dumps capturados pelo PCSX2 (ignorado no Git)
│       ├── replacements/          # Texturas [hash].png para o PCSX2 (ignorado no Git)
│       ├── texture_map.json       # Mapeamento de custom_character/ -> [hash].png
│       └── README_PCSX2_TEXTURES.md
├── scripts/
│   ├── 01_inspect_and_extract.py  # Etapa 1: Inspeção read-only e extração via pycdlib
│   ├── 02_decode_sprites.py       # Etapa 2: Decoder de sprites 4bpp/LZ + paletas FIRST.GRC
│   ├── 03_build_mod_iso.py        # Etapa 3: Re-encoder, patch LBA-safe e recálculo EDC/ECC
│   ├── 04_pcsx2_texture_helper.py # Etapa 4: Helper de Texture Replacement do PCSX2 Qt
│   └── iso_manifest.json          # Manifesto com LBA, tamanho em bytes e SYSTEM.CNF
└── README.md
```

---

## Pré-requisitos e Setup (`.venv`)

Python 3.12+ (Windows PowerShell):

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install pycdlib Pillow numpy
```

Para confirmar as dependências sem ativar o ambiente:

```powershell
.venv\Scripts\python.exe -c "import pycdlib, PIL, numpy; print('OK')"
```

---

## Pipeline Completo (5 Etapas)

### Etapa 1 — Inspeção e Extração do Disco (`scripts/01_inspect_and_extract.py`)

Lê a imagem `.bin` (2352 bytes/setor, Mode 2) ou `.iso` (2048 bytes/setor) de forma **estritamente somente leitura** via `pycdlib` + `RawBinSectorReader`, extrai o `SYSTEM.CNF` (`BOOT2 = cdrom0:\SLES_533.81;1`), gera `scripts/iso_manifest.json` com o `lba_start` e `size_bytes` exatos de todos os arquivos e extrai o conteúdo para `extracted/`.

```powershell
.venv\Scripts\python.exe scripts/01_inspect_and_extract.py
```

Flags disponíveis:
- `--iso <CAMINHO>`: caminho para `.iso` ou `.bin` (se omitido, detecta automaticamente em `G:\KOF`).
- `--out-dir <DIR>`: diretório de saída para os arquivos extraídos (padrão: `extracted/`).
- `--manifest <ARQ>`: caminho de saída do manifesto JSON (padrão: `scripts/iso_manifest.json`).
- `--no-extract`: apenas inspeciona a imagem e grava `scripts/iso_manifest.json` sem extrair arquivos.

---

### Etapa 2 — Decodificação de Sprites e Paletas (`scripts/02_decode_sprites.py`)

Decodifica os bancos de sprites de personagens (`/DATA/PL0/PXX.BIN` ou `/DATA/PL1/PXX.BIN` — ex.: **Omega Rugal** em `/DATA/PL1/P27.BIN`, ID `0x27`, LBA `25769`, `642` frames) e as paletas RGB555 de `/DATA/BASE/FIRST.GRC` (referenciadas pela tabela em `SLES_533.81` offset `0x3e6960`).

Exporta frames individuais em `sprites_dump/<char_slug>/frames/frame_XXXX.png` (PNG **Indexado 16 cores**, `mode='P'`, índice `0` transparente), folhas de contato (`sheet_00.png`, ...), comparativo de botões (`palettes_ABCD.png`) e o mapa técnico `sprites_dump/offsets_map.json`.

```powershell
# Exemplo padrão: decodifica Omega Rugal (P27) com paleta do botão A
.venv\Scripts\python.exe scripts/02_decode_sprites.py --char rugal --btn A

# Exportar também contact sheets dos 4 botões (A/B/C/D) e subsets EPL0/EPL1
.venv\Scripts\python.exe scripts/02_decode_sprites.py --char rugal --all-buttons --include-epl
```

Flags disponíveis:
- `--char <NOME|ID>`: personagem por nome ou ID (`rugal`, `P27`, `P00`..`P32`, `0x27`; padrão: `rugal`).
- `--btn {A,B,C,D}`: paleta do botão `A`, `B`, `C` ou `D` (padrão: `A`).
- `--all-buttons`: gera também contact sheets para os 4 botões (`A`, `B`, `C`, `D`).
- `--include-epl`: decodifica também os arquivos `EPL0`/`EPL1` correspondentes.
- `--extracted-dir <DIR>`: diretório dos arquivos extraídos (padrão: `extracted/`).
- `--manifest <ARQ>`: caminho do `iso_manifest.json` (padrão: `scripts/iso_manifest.json`).
- `--out-dir <DIR>`: diretório de saída (padrão: `sprites_dump/`).
- `--no-sheets`: pula a geração de contact sheets (gera apenas frames individuais, `palettes_ABCD.png` e `offsets_map.json`).

---

### Etapa 3 — Edição no LibreSprite / Aseprite (`custom_character/`)

Copie apenas os frames que deseja modificar de `sprites_dump/rugal_P27/frames/` para `custom_character/` (mantendo o nome `frame_XXXX.png` ou `frame_XXXX_descricao.png`) e edite seguindo **obrigatoriamente** estas regras para reinjeção na ISO:

1. **Modo de Cor:** **Indexed Color** (`Sprite -> Color Mode -> Indexed` no LibreSprite/Aseprite; `mode == 'P'` no PNG). **Não** salve em RGBA/RGB para o build da ISO.
2. **Máximo de 16 cores:** utilize apenas os **índices `0` a `15`** da paleta.
3. **Transparência no Índice 0:** o índice `0` da paleta deve permanecer como cor transparente (`tRNS`).
4. **Dimensões exatas:** largura e altura (`width_px x height_px`) devem ser idênticas às do frame original registradas em `sprites_dump/offsets_map.json`.
5. **Troca de Paleta (opcional):** caso altere as cores `1..15` da paleta do PNG, passe `--update-palette` na Etapa 4 para atualizar também `/DATA/BASE/FIRST.GRC`.

---

### Etapa 4 — Reconstrução e Patch LBA-Safe da ISO/BIN (`scripts/03_build_mod_iso.py`)

Lê `sprites_dump/offsets_map.json` e `scripts/iso_manifest.json`, valida cada PNG em `custom_character/` (`mode == 'P'`, índices `0..15`, dimensões exatas), re-codifica os tiles `16x16` 4bpp column-major (`flg=0x01` sem compressão ou `flg=0x03` com compressão LZ), grava os arquivos patcheados em `build/patched_files/` respeitando o tamanho original do arquivo (`1.604.064` bytes no `P27.BIN`, fazendo patch in-place ou repack seguro) e aplica o patch apenas nos setores LBA correspondentes sobre uma cópia em `build/kof2002_mod.bin` + `build/kof2002_mod.cue` (ou `.iso` se a entrada for 2048B/setor).

Em imagens **Mode 2 / 2352** (`.bin` + `.cue`), o script recalcula os **4 bytes de EDC (ECMA-130)** e os **276 bytes de ECC Reed-Solomon (P/Q)** de cada setor modificado, preservando integridade total de leitura no PS2 e no PCSX2.

```powershell
# Gerar build/kof2002_mod.bin + build/kof2002_mod.cue
.venv\Scripts\python.exe scripts/03_build_mod_iso.py

# Incluir atualização de paleta (cores 1..15) no FIRST.GRC
.venv\Scripts\python.exe scripts/03_build_mod_iso.py --update-palette

# Verificar integridade EDC/ECC, bytes patcheados e setores intactos
.venv\Scripts\python.exe scripts/03_build_mod_iso.py --verify-only
```

Flags disponíveis:
- `--iso <ARQ>`: imagem `.bin` ou `.iso` original (padrão: lido de `scripts/iso_manifest.json`).
- `--cue <ARQ>`: arquivo `.cue` original correspondente.
- `--manifest <ARQ>`: caminho para `scripts/iso_manifest.json`.
- `--offsets-map <ARQ>`: caminho para `sprites_dump/offsets_map.json`.
- `--custom-dir <DIR>`: diretório com os PNGs editados (padrão: `custom_character/`).
- `--extracted-dir <DIR>`: diretório `extracted/` somente leitura (padrão: `extracted/`).
- `--patched-dir <DIR>`: pasta de saída dos `.BIN`/`.GRC` patcheados (padrão: `build/patched_files/`).
- `--output <ARQ>`: caminho do disco modificado (padrão: `build/kof2002_mod.bin` + `.cue`, ou `.iso`).
- `--update-palette`: atualiza também as cores `1..15` da paleta em `/DATA/BASE/FIRST.GRC` se diferirem de `offsets_map.json`.
- `--verify-only`: valida o disco gerado em `--output` contra `build/patched_files/` e verifica EDC/ECC e setores não modificados.

---

### Etapa 5 — Helper de Texture Replacement no PCSX2 Qt (`scripts/04_pcsx2_texture_helper.py`)

Como alternativa rápida à reconstrução da ISO (ou para packs HD em RGBA), o **PCSX2 Qt (v2.8.2)** suporta substituição dinâmica de texturas via `textures/SLES-53381/replacements/` (convertendo automaticamente `SLES_533.81` do `scripts/iso_manifest.json` para `SLES-53381`).

```powershell
# 1. Inicializar estrutura textures/SLES-53381/ (replacements/, dumps/, texture_map.json e guia)
.venv\Scripts\python.exe scripts/04_pcsx2_texture_helper.py init

# 2. Validar PNGs customizados (RGBA/Indexado, paleta, potência de 2, escala inteira vs dumps/)
.venv\Scripts\python.exe scripts/04_pcsx2_texture_helper.py validate --source custom_character

# 3. Empacotar/copiar substituições mapeadas ou já nomeadas com hash para replacements/ e sincronizar com o PCSX2
.venv\Scripts\python.exe scripts/04_pcsx2_texture_helper.py stage --source custom_character --pcsx2-dir "C:\Caminho\Para\PCSX2"

# 4. Validar a pasta final replacements/ (exige nomes no padrão de hash do PCSX2)
.venv\Scripts\python.exe scripts/04_pcsx2_texture_helper.py validate
```

Flags globais e subcomandos:
- Globais: `--manifest <ARQ>`, `--serial <SERIAL>` (override manual, ex.: `SLES-53381`), `--base-dir <DIR>`.
- `init`: `--pcsx2-dir <DIR>`, `--force`.
- `validate`: `--source <DIR|PNG>`, `--ref-dir <DIR>`, `--require-hash` / `--no-require-hash`, `--strict`, `--fail-on-empty`, `--json`.
- `stage` (ou alias `pack`): `--source <DIR>`, `--map <JSON>`, `--dest <DIR>`, `--dumps-dir <DIR>`, `--pcsx2-dir <DIR>`, `--strict`, `--dry-run`.

#### Fluxo rápido no PCSX2 Qt v2.8.2 (`pcsx2-qt.exe`)
1. Em **Settings -> Graphics -> Texture Replacement**, ative **Dump Textures** e jogue para gerar os arquivos `[hash].png` em `textures/SLES-53381/dumps/`.
2. Edite os PNGs mantendo o nome exato `[hash].png` (ou configure os recortes/hashes em `textures/SLES-53381/texture_map.json`).
3. Execute `validate` e `stage --pcsx2-dir <pasta_do_PCSX2>`.
4. Desative **Dump Textures**, ative **Load Textures** (e opcionalmente **Async Texture Loading**) e reinicie ou recarregue as texturas no jogo.
