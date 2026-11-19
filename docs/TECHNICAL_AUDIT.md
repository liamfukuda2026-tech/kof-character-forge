# Auditoria Técnica — KOF Character Forge AI
**The King of Fighters 2002 (PlayStation 2 — Europa, `SLES_533.81` / `SLES-53381`)**  
**Data:** 19 de Novembro de 2026 | **Classificação:** Estritamente Baseado em Evidências

---

## 1. Identificação do Alvo e Ambiente Técnico

- **Título Oficial:** The King of Fighters 2002
- **Plataforma:** Sony PlayStation 2 (PS2)
- **Região / Serial:** Europa / `SLES_533.81` (normalizado no PCSX2 como `SLES-53381`)
- **Identificador de Boot (`SYSTEM.CNF`):** `BOOT2 = cdrom0:\SLES_533.81;1; VER = 1.00; VMODE = PAL`
- **Mídia Física Original:** Imagem de CD-ROM binária crua (`King of Fighters 2002, The (Europe).bin` + `.cue`)
- **Geometria de Setor:** CD-ROM XA Mode 2 Form 1 (2.352 bytes brutos/setor; 2.048 bytes de dados de usuário no offset `+24`)
- **Tamanho Total da Imagem Original:** 446.463.696 bytes (189.823 setores físicos)
- **Tamanho do Executável Principal (`SLES_533.81`):** 4.199.640 bytes (LBA inicial: 47, 2.051 setores)
- **Contêiner de Áudio (`/ADX/BGM.AFS`):** 170.283.008 bytes (LBA inicial: 84.995, 83.146 setores)
- **Contêiner Gráfico Base (`/DATA/BASE/FIRST.GRC`):** 2.147.502 bytes (LBA inicial: 74.747, 1.049 setores)
- **Total de Arquivos no Sistema de Arquivos ISO9660:** 392 arquivos indexados

---

## 2. Matriz Estrita de Classificação dos Sistemas

Conforme as diretrizes arquiteturais, nenhum recurso visual no navegador ou no emulador é presumido como gravável na ISO sem comprovação matemática e binária. A classificação segue quatro níveis rigorosos:

| Sistema / Subsistema | Status | Leitura Confirmada | Escrita Confirmada | Localização no Disco / Memória | Notas de Engenharia e Limitações |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Leitor de Setores Mode 2 Form 1** | **CONFIRMADO** | Sim (`pycdlib` + `RawBinSectorReader`) | Sim (`rebuild_mode2_form1_sector`) | Setores físicos 0..189822 | Decodifica `+24`, recalcula EDC (ECMA-130) e ECC Reed-Solomon P/Q (172B + 104B). |
| **Banco de Sprites dos Lutadores (`PXX.BIN`)** | **CONFIRMADO** | Sim (`02_decode_sprites.py`) | Sim (`03_build_mod_iso.py`) | `/DATA/PL0/PXX.BIN` e `/DATA/PL1/PXX.BIN` | Tiles 16x16 4bpp column-major. Suporta uncompressed (`flg=0x01`) e SNK-LZ (`flg=0x03`). |
| **Tabela de Paletas dos Lutadores** | **CONFIRMADO** | Sim (`read_char_base_palettes`) | Sim (`patch` / in-place) | `SLES_533.81` offset `0x3E6960` | Tabela de 64 entradas de 8 bytes (4 `uint16` little-endian para botões A, B, C, D). |
| **Banco de Cores RGB555 (`FIRST.GRC`)** | **CONFIRMADO** | Sim (`load_first_grc_palettes`) | Sim (`--update-palette` em 03) | `/DATA/BASE/FIRST.GRC` sub-bloco [1] | 16 cores por paleta (32 bytes). Cor 0 transparente. 15 cores ativas. |
| **Bancos de Efeitos (`EPL0`..`EPL3`)** | **CONFIRMADO** | Sim (`find_char_bin` com prefixo) | Sim (in-place LBA) | `/DATA/EPL0`..`/DATA/EPL3` | Mesma estrutura de container gráfico de `PXX.BIN`. |
| **Banco de Vozes (`pXX.acx` em `BGM.AFS`)** | **CONFIRMADO** | Sim (`_read_char_acx`) | Sim (`06_build_custom_voices.py`) | `/ADX/BGM.AFS` entrada AFS #73 (`p27.acx`) | CRI ADX v4 24kHz mono empacotado em container ACX alinhado a 4 bytes. |
| **Editor de Arquivos Offline do Disco** | **CONFIRMADO** | Sim (`07_disc_editor.py`) | Sim (`07_disc_editor.py`) | Qualquer um dos 392 arquivos da ISO | Patch LBA-safe (`tamanho_editado <= tamanho_original`). Recálculo de EDC/ECC. |
| **Substituição de Texturas no PCSX2** | **CONFIRMADO** | Sim (`04_pcsx2_texture_helper.py`) | N/A (Emulador) | `textures/SLES-53381/replacements/` | Baseado em injeção em tempo de execução via hash do GS do PCSX2 Qt v2.8.2. |
| **Player Heurístico de Animações** | **PARCIAL** | Sim (`_slice_sequences` no Studio) | Não (Não exporta lógica de animação) | Interface Web (`08_web_character_studio.py`) | Segmenta frames consecutivos por proximidade de bbox; não lê scripts do motor. |
| **Presets de Golpes Especiais (SP1..MAX2)** | **PARCIAL** | Sim (Heurística visual no Studio) | Não (Não exporta tabelas de colisão) | Interface Web (`08_web_character_studio.py`) | Agrupa frames de efeitos gráficos e sincroniza com áudio no navegador. |
| **Lista de Movimentos (`SKILL.DAT`)** | **PARCIAL** | Sim (Tabela de ponteiros uint32) | Não | `/DATA/SKILL.DAT` (59.530 bytes, LBA 72.335) | Ponteiros de comandos para modo Practice/demo de golpes; estrutura interna não mapeada. |
| **Caixas de Colisão e Hitboxes (Push/Hurt/Hit)** | **NÃO YET DECODED** | Não | Não | `SLES_533.81` / `GAME.BIN` | Não existem dados de colisão dentro de `PXX.BIN`. Localizados no executável ou engine. |
| **Tabelas de Execução de Comandos (State Machine)** | **NÃO YET DECODED** | Não | Não | `SLES_533.81` / `/DATA/BASE/GAME.BIN` | Máquina de estados que processa `QCF+P`, `DP+K`, buffers e cancelamentos. |
| **Retratos da Seleção de Personagens** | **NÃO YET DECODED** | Não | Não | `/DATA/BASE/PSEL.BIN` e `DATA/ART` | Mapeamento de coordenadas dos ícones e portraits na tela de seleção. |
| **Motor Central em Jogo (`GAME.BIN`)** | **NÃO YET DECODED** | Não | Não | `/DATA/BASE/GAME.BIN` (2.453.584 bytes, LBA 75.796)| Arquivo monolítico com dados compilados do motor NeoGeo/PS2. Sub-tabelas não decodificadas. |
| **Criação de 47º Slot Adicional (Inédito)** | **NÃO YET DECODED** | Não | Não | Tabela ISO9660 + `SLES_533.81` | Exige reestruturação do descritor de volume ISO e expansão de limites de memória. |

---

## 3. Análise Detalhada dos Scripts Existentes (01 a 08)

### Script 01: `01_inspect_and_extract.py` (16.804 bytes)
- **Função:** Inspeciona discos no formato Mode 2 Form 1 (`2352` bytes/setor) e ISO9660 padrão (`2048` bytes/setor) sem modificar a mídia.
- **Classes Principais:** `RawBinSectorReader(io.RawIOBase)` implementa paginação lógica transparente transformando blocos físicos de 2.352 bytes em blocos virtuais de 2.048 bytes (offset `+24`).
- **Geração de Manifesto:** Varre a árvore de diretórios via `pycdlib` e gera `scripts/iso_manifest.json` com `lba_start`, `lba_sectors`, `size_bytes` e caminho relativo normalizado de todos os 392 arquivos.
- **Segurança:** O leitor opera estritamente em modo `readable() == True` e `writable() == False`.

### Script 02: `02_decode_sprites.py` (28.126 bytes)
- **Função:** Decodificador dos bancos gráficos de lutadores (`DATA/PL0/PXX.BIN` e `DATA/PL1/PXX.BIN`).
- **Formato Gráfico Decodificado:**
  - Cabeçalho do bloco de frame: 8 bytes (`hdr_sz: uint32`, `pal_slot: uint8`, `w_tiles: uint8`, `h_tiles: uint8`, `flg: uint8`).
  - Bitmask de presença de tiles: bitmask column-major indicando tiles 16x16 ativos.
  - Payload de tiles: 4bpp (2 pixels por byte, nibble baixo = pixel par, nibble alto = pixel ímpar), 128 bytes por tile 16x16.
  - Modos de compressão: `flg=0x01` (não comprimido) e `flg=0x03` (algoritmo proprietário SNK-LZ).
- **Decodificador de Paletas:** Lê o offset `0x3E6960` em `SLES_533.81` para localizar os índices no sub-bloco [1] de `/DATA/BASE/FIRST.GRC`. Converte palavras de 16 bits (RGB555 little-endian) em RGBA de 32 bits, preservando o índice 0 como transparente.

### Script 03: `03_build_mod_iso.py` (45.388 bytes)
- **Função:** Mecanismo de re-encode, verificação e gravação segura na imagem de disco.
- **Motor Matemático de Integridade CD-ROM:**
  - `compute_mode2_form1_edc()`: Polinômio CRC de 32 bits sobre `sector[16:2072]` conforme a norma ECMA-130.
  - `compute_mode2_form1_ecc()`: Reed-Solomon Cross-Interleaved (cálculo de paridades P de 172 bytes e Q de 104 bytes sobre campos de Galois GF($2^8$)).
  - `rebuild_mode2_form1_sector()`: Monta o setor bruto de 2.352 bytes preservando sincronismo (`00 FF..FF 00`), cabeçalho e sub-cabeçalho.
- **Re-compressor LZ:** `lz_compress()` empacota os tiles editados com busca de retro-referências de janela deslizante (limite de offset 255 bytes, comprimento até 63 bytes), terminando com byte `0xC0`.
- **Invariante LBA-Safe:** Substituição de setores in-place diretamente em `build/kof2002_mod.bin`. Setores inalterados permanecem intactos bit-a-bit.

### Script 04: `04_pcsx2_texture_helper.py` (34.749 bytes)
- **Função:** Gerenciador do subsistema de Texture Replacement do PCSX2 Qt (v2.8.2).
- **Validação de Texturas:** Verifica dimensões potência de 2, modos suportados (`RGBA`, `P`), integridade da paleta e hashes hexadecimais compatíveis com o emulador.
- **Staging:** Mapeia arquivos de `custom_character/` para `textures/SLES-53381/replacements/[hash].png`.

### Script 05: `05_prepare_custom_frames.py` (58.940 bytes)
- **Função:** Pipeline de preparação de sprites gerados por IA ou importados externamente.
- **Processamento de Imagem:**
  - Remoção de fundo chroma-key magenta (`#FF00FF`) com tolerância euclidiana e limpeza de halos.
  - Auto-fatiamento de spritesheets usando componentes conexos 8-vizinhos em NumPy puro.
  - Alinhamento bottom-center (ancoragem pelos pés) baseado na caixa delimitadora do sprite original.
  - Quantização estrita para paleta compartilhada de 15 cores opacas + índice 0 transparente.

### Script 06: `06_build_custom_voices.py` (17.579 bytes)
- **Função:** Gerador e injetor de pacotes de voz customizados para o slot do Rugal (`p27.acx`).
- **Conversão de Áudio:** Transforma MP3/WAV em CRI ADX v4 (24.000 Hz, 1 canal mono, ADPCM preditivo de 4 bits).
- **Empacotamento ACX:** Gera o contêiner CRI ACX alinhado a 4 bytes e injeta os 342 setores correspondentes dentro de `/ADX/BGM.AFS` no offset `0x9B8C000`.

### Script 07: `07_disc_editor.py` (18.512 bytes)
- **Função:** Editor de arquivos offline em linha de comando e GUI Tkinter.
- **Controle de Espaço de Trabalho:** Permite extrair, modificar e reinjetar qualquer um dos 392 arquivos da ISO, gravando os arquivos patcheados em `workspace/files/` e reconstruindo o disco via Script 03.

### Script 08: `08_web_character_studio.py` (92.043 bytes)
- **Função:** Servidor Web local completo (`http://127.0.0.1:8765`) com interface moderna em Canvas 2D.
- **Recursos Comprovados no Código:**
  - Carregamento de miniaturas e visualização dos 46 lutadores do jogo.
  - Reprodução de animações com ajuste de FPS e controle de looping.
  - Visualizador e editor de paletas de cores A/B/C/D integrado com `/DATA/BASE/FIRST.GRC`.
  - Player de áudio ADX/WAV integrado diretamente no navegador via decodificador PCM nativo em Python.
  - Navegador hexadecimal de arquivos do disco com capacidade de patch in-place.
  - Gatilho direto para inicialização do PCSX2.

---

## 4. Mapa Geométrico de Alocação de Setores (LBA)

No padrão ISO9660 em mídia Mode 2 Form 1, os setores lógicos têm exatamente 2.048 bytes. A tabela a seguir documenta todos os 46 slots de personagens presentes no jogo original, seus limites exatos em bytes e setores LBA:

### Tabela Completa de Lutadores (`DATA/PL0` e `DATA/PL1`)

| Slot ID | Nome do Lutador | Caminho na ISO | Tamanho (Bytes) | Setores | LBA Início | LBA Fim |
| :---: | :--- | :--- | :---: | :---: | :---: | :---: |
| **0x00** | Kyo Kusanagi | `/DATA/PL0/P00.BIN` | 1.241.024 | 606 | 29.558 | 30.163 |
| **0x01** | Benimaru Nikaido | `/DATA/PL0/P01.BIN` | 835.272 | 408 | 30.164 | 30.571 |
| **0x02** | Goro Daimon | `/DATA/PL0/P02.BIN` | 867.016 | 424 | 30.572 | 30.995 |
| **0x03** | **Terry Bogard** *(Base)* | `/DATA/PL0/P03.BIN` | **1.034.776** | **506** | **30.996** | **31.501** |
| **0x04** | Andy Bogard | `/DATA/PL0/P04.BIN` | 999.680 | 489 | 31.502 | 31.990 |
| **0x05** | Joe Higashi | `/DATA/PL0/P05.BIN` | 1.052.016 | 514 | 31.991 | 32.504 |
| **0x06** | Ryo Sakazaki | `/DATA/PL0/P06.BIN` | 923.768 | 452 | 32.505 | 32.956 |
| **0x07** | Robert Garcia | `/DATA/PL0/P07.BIN` | 1.321.840 | 646 | 32.957 | 33.602 |
| **0x08** | Yuri Sakazaki | `/DATA/PL0/P08.BIN` | 756.152 | 370 | 33.603 | 33.972 |
| **0x09** | Leona Heidern | `/DATA/PL0/P09.BIN` | 1.217.576 | 595 | 33.973 | 34.567 |
| **0x0A** | Ralf Jones | `/DATA/PL0/P0A.BIN` | 1.070.376 | 523 | 34.568 | 35.090 |
| **0x0B** | Clark Still | `/DATA/PL0/P0B.BIN` | 943.744 | 461 | 35.091 | 35.551 |
| **0x0C** | Athena Asamiya | `/DATA/PL0/P0C.BIN` | 1.223.544 | 598 | 35.552 | 36.149 |
| **0x0D** | Sie Kensou | `/DATA/PL0/P0D.BIN` | 960.384 | 469 | 36.150 | 36.618 |
| **0x0E** | Chin Gentsai | `/DATA/PL0/P0E.BIN` | 956.776 | 468 | 36.619 | 37.086 |
| **0x0F** | Chizuru Kagura | `/DATA/PL0/P0F.BIN` | 827.080 | 404 | 37.087 | 37.490 |
| **0x10** | Mai Shiranui | `/DATA/PL0/P10.BIN` | 853.040 | 417 | 37.491 | 37.907 |
| **0x11** | Kim Kaphwan | `/DATA/PL0/P11.BIN` | 977.720 | 478 | 37.908 | 38.385 |
| **0x12** | Chang Koehan | `/DATA/PL0/P12.BIN` | 1.279.048 | 625 | 38.386 | 39.010 |
| **0x13** | Choi Bounge | `/DATA/PL0/P13.BIN` | 891.520 | 436 | 39.011 | 39.446 |
| **0x14** | Iori Yagami | `/DATA/PL0/P14.BIN` | 1.690.136 | 826 | 39.447 | 40.272 |
| **0x15** | Mature | `/DATA/PL1/P15.BIN` | 1.260.336 | 616 | 14.394 | 15.009 |
| **0x16** | Vice | `/DATA/PL1/P16.BIN` | 998.784 | 488 | 15.010 | 15.497 |
| **0x17** | Ryuji Yamazaki | `/DATA/PL1/P17.BIN` | 850.448 | 416 | 15.498 | 15.913 |
| **0x18** | Blue Mary | `/DATA/PL1/P18.BIN` | 1.377.144 | 673 | 15.914 | 16.586 |
| **0x19** | Billy Kane | `/DATA/PL1/P19.BIN` | 1.137.808 | 556 | 16.587 | 17.142 |
| **0x1A** | Yashiro Nanakase | `/DATA/PL1/P1A.BIN` | 1.541.344 | 753 | 17.143 | 17.895 |
| **0x1B** | Shermie | `/DATA/PL1/P1B.BIN` | 1.290.400 | 631 | 17.896 | 18.526 |
| **0x1C** | Chris | `/DATA/PL1/P1C.BIN` | 1.627.456 | 795 | 18.527 | 19.321 |
| **0x1D** | K' | `/DATA/PL1/P1D.BIN` | 1.461.640 | 714 | 19.322 | 20.035 |
| **0x1E** | Maxima | `/DATA/PL1/P1E.BIN` | 1.307.208 | 639 | 20.036 | 20.674 |
| **0x1F** | Whip | `/DATA/PL1/P1F.BIN` | 1.680.496 | 821 | 20.675 | 21.495 |
| **0x20** | Vanessa | `/DATA/PL1/P20.BIN` | 1.746.456 | 853 | 21.496 | 22.348 |
| **0x21** | Seth | `/DATA/PL1/P21.BIN` | 1.078.056 | 527 | 22.349 | 22.875 |
| **0x22** | Ramon | `/DATA/PL1/P22.BIN` | 1.266.336 | 619 | 22.876 | 23.494 |
| **0x23** | Kula Diamond | `/DATA/PL1/P23.BIN` | 729.544 | 357 | 23.495 | 23.851 |
| **0x24** | K9999 | `/DATA/PL1/P24.BIN` | 1.466.248 | 716 | 23.852 | 24.567 |
| **0x25** | Angel | `/DATA/PL1/P25.BIN` | 1.069.880 | 523 | 24.568 | 25.090 |
| **0x26** | Bao / Extra | `/DATA/PL1/P26.BIN` | 1.387.360 | 678 | 25.091 | 25.768 |
| **0x27** | Omega Rugal | `/DATA/PL1/P27.BIN` | 1.604.064 | 784 | 25.769 | 26.552 |
| **0x28** | Kusanagi | `/DATA/PL1/P28.BIN` | 998.760 | 488 | 26.553 | 27.040 |
| **0x2E** | Shingo Yabuki | `/DATA/PL1/P2E.BIN` | 912.672 | 446 | 27.041 | 27.486 |
| **0x2F** | King | `/DATA/PL1/P2F.BIN` | 611.416 | 299 | 27.487 | 27.785 |
| **0x30** | Orochi Iori | `/DATA/PL1/P30.BIN` | 1.038.952 | 508 | 27.786 | 28.293 |
| **0x31** | Geese Howard | `/DATA/PL1/P31.BIN` | 1.431.784 | 700 | 28.294 | 28.993 |
| **0x32** | Goenitz | `/DATA/PL1/P32.BIN` | 1.154.152 | 564 | 28.994 | 29.557 |

> [!IMPORTANT]
> **Invariante de Contiguidade:** Não existe nenhum setor livre entre os arquivos de lutadores. Por exemplo, o fim de `P03.BIN` (Terry) no LBA 31.501 conecta-se diretamente ao início de `P04.BIN` no LBA 31.502. Qualquer patch que aumente o tamanho do arquivo corrompe o cabeçalho do lutador seguinte.

---

## 5. Engenharia Reversa do Executável `SLES_533.81`

### Tabela de Paletas dos Lutadores (`0x3E6960`)
O executável contém uma tabela global de índices de paleta localizada no offset de arquivo `0x3E6960`. Cada lutador ocupa exatamente 8 bytes, correspondendo a 4 inteiros de 16 bits sem sinal (`uint16` little-endian) representando os índices dos botões **A, B, C e D**:

$$\text{Offset}(cid) = 0x3E6960 + (cid \times 8)$$

- **Entrada Terry Bogard ($cid = 0x03$):** Offset `0x3E6978`
  - Botão A: `448` (`0x01C0`)
  - Botão B: `464` (`0x01D0`)
  - Botão C: `480` (`0x01E0`)
  - Botão D: `496` (`0x01F0`)
- **Entrada Omega Rugal ($cid = 0x27$):** Offset `0x3E6A98`
  - Botão A: `2752` (`0x0AC0`)
  - Botão B: `2768` (`0x0AD0`)
  - Botão C: `2784` (`0x0AE0`)
  - Botão D: `2800` (`0x0AF0`)

Cada índice $N$ aponta para o bloco de 32 bytes dentro do sub-bloco [1] de `/DATA/BASE/FIRST.GRC`:

$$\text{Offset no Sub-bloco}[1] = N \times 32\text{ bytes}$$

---

## 6. Riscos de Corrupção e Mitigações

1. **Dessincronização de EDC/ECC Mode 2 Form 1:**
   - *Risco:* Gravar apenas os 2.048 bytes de dados de usuário deixa o EDC e o ECC Reed-Solomon corrompidos. O leitor óptico do PS2 real congela ou aciona tela preta no carregamento da luta.
   - *Mitigação:* Uso obrigatório de `rebuild_mode2_form1_sector()` com recálculo simultâneo do polinômio CRC de 32 bits e das matrizes P/Q de paridade sobre GF($2^8$).
2. **Estouro do Limite de Setores (LBA Overrun):**
   - *Risco:* Se os sprites re-comprimidos de Terry Legendary ultrapassarem 1.034.776 bytes (506 setores), a gravação sobrescreverá os dados de `P04.BIN` (Andy Bogard).
   - *Mitigação:* Validador atômico pré-build. Se o payload exceder o teto, o build é rejeitado e o usuário é orientado a otimizar áreas vazias ou reduzir o fatiamento.
3. **Corrupção do Byte de Término do Compressor LZ:**
   - *Risco:* Falta do terminador `0xC0` ou offset de retro-referência maior que o buffer de saída já descompactado causa loop infinito ou travamento de ponteiro na CPU MIPS R5900 (Emotion Engine).
   - *Mitigação:* Suíte de testes com round-trip obrigatório: `lz_decompress(lz_compress(dados)) == dados`.
4. **Violação do Índice 0 da Paleta:**
   - *Risco:* Colocar cor sólida no índice 0 torna o sprite um retângulo opaco cobrindo o cenário.
   - *Mitigação:* `05_prepare_custom_frames.py` e o validador de skills forçam o índice 0 como `(255, 0, 255)` com tag `tRNS = 0`.

---

## 7. Módulos Reutilizáveis para o Core do Character Forge

Os seguintes módulos existentes devem ser modularizados em `G:\KOF\core/` sem duplicação de lógica:
1. `core.iso.sector_engine`: Extraído de `03_build_mod_iso.py` (`rebuild_mode2_form1_sector`, `compute_mode2_form1_edc`, `compute_mode2_form1_ecc`).
2. `core.sprites.lz_codec`: Extraído de `03_build_mod_iso.py` (`lz_compress`, `lz_decompress`).
3. `core.sprites.tile_codec`: Extraído de `02_decode_sprites.py` e `03_build_mod_iso.py` (decodificação e codificação de tiles 16x16 4bpp column-major).
4. `core.palettes.first_grc`: Extraído de `02_decode_sprites.py` (leitura, escrita e remapeamento de paletas RGB555).
5. `core.audio.adx_packer`: Extraído de `06_build_custom_voices.py` (encoder CRI ADX v4 e CRI ACX).
6. `core.project.forge_project`: Gerenciamento do ciclo de vida de projetos em `workspace/forge/<project_name>/`.

---

## 8. Mapeamento das 18 Fases do Projeto

1. **Fase 1 — Auditoria Técnica:** Documento `docs/TECHNICAL_AUDIT.md` (este documento).
2. **Fase 2 — Arquitetura Modular:** `docs/ARCHITECTURE.md` definindo camadas desacopladas.
3. **Fase 3 — Character Studio:** Botão `[ + CRIAR PERSONAGEM ]` e suporte ao Modo B (Base Terry Bogard).
4. **Fase 4 — Editor Visual de Sprites:** Importação de PNG/spritesheet, alinhamento automático e quantização de paleta.
5. **Fase 5 — Editor de Animações:** Timeline com 14 categorias canônicas (Idle, Walk, Special, etc.).
6. **Fase 6 — Editor de Golpes:** Modelagem de ataques com avisos explícitos de não-exportação para recursos não decodificados.
7. **Fase 7 — Agente de Inteligência Artificial:** Forge AI Assistant com chamadas estruturadas de ferramentas.
8. **Fase 8 — Sistema de Skills:** Biblioteca de habilidades determinísticas documentada em `docs/SKILLS.md`.
9. **Fase 9 — Base de Conhecimento:** Repositório técnico com status de evidência.
10. **Fase 10 — Integração com Modelos de IA:** Provedores de LLM configuráveis sem chaves fixas no código.
11. **Fase 11 — Geração de Imagens com IA:** Distinção entre concept art, sprite e frame de animação.
12. **Fase 12 — Validador Automático:** Verificador com 4 estados (Compatível, Atenção, Incompatível, Não Verificado).
13. **Fase 13 — Construção e Testes:** Pipeline automatizado: Validar $\to$ Backup $\to$ Patch $\to$ EDC/ECC $\to$ PCSX2.
14. **Fase 14 — Formato de Personagem (`.kofchar`):** Empacotamento ZIP com `character.json`.
15. **Fase 15 — Instalador para Windows:** Empacotamento limpo e redistribuível sem assets proprietários inclusos.
16. **Fase 16 — GitHub e Distribuição:** Documentação completa para publicação open-source.
17. **Fase 17 — Primeiro Teste Real (Terry Legendary):** Substituição controlada de frames do Terry Bogard (P03).
18. **Fase 18 — Ordem de Execução:** Sequência metódica de 12 passos garantindo fundação antes de expansão.
