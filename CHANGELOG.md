# Changelog — KOF Character Forge AI

Todas as alterações notáveis neste projeto serão documentadas neste arquivo.

O formato é baseado em [Keep a Changelog](https://keepachangelog.com/pt-BR/1.0.0/),
e este projeto adere ao [Versionamento Semântico](https://semver.org/lang/pt-BR/).

---

## [0.1.0] — 2026-11-19

### Adicionado (Added)
- **Documentação de Engenharia e Auditoria:**
  - `docs/TECHNICAL_AUDIT.md`: Diagnóstico completo do repositório, classificação formal de capacidades (*CONFIRMADO*, *PARCIAL*, *NÃO IMPLEMENTADO*, *NÃO DECODIFICADO*), mapa de setores LBA dos 46 lutadores e cronograma detalhado das 18 fases.
  - `docs/ARCHITECTURE.md`: Especificação da arquitetura modular do Character Forge, formato de projeto `.kofchar` com manifesto `character.json`, fluxogramas Mermaid e protocolo de injeção LBA-safe.
  - `docs/SKILLS.md`: Catálogo de 10 skills determinísticas com esquemas JSON strongly-typed e estratégias de reversão atômica.
  - `docs/COMPATIBILITY.md`: Matriz de compatibilidade de edições (homologação de `SLES_533.81` PAL contra `SLPS_253.45`, `SLUS_208.31`, PC Unlimited Match e ROMs NeoGeo).
  - `docs/AI_AGENT.md`: Protocolo do Forge AI Assistant, definição de chamadas estruturadas de ferramentas (Tool Calling) e sandboxing.
  - `docs/INSTALLATION.md`: Manual passo a passo para configuração em Windows com Python 3.12, ambiente virtual `.venv` e integração com PCSX2 Qt v2.8.2.
  - `CONTRIBUTING.md`: Guia de contribuição com diretrizes éticas, conformidade de direitos autorais e suítes de testes.
- **Mecanismos e Scripts de Referência:**
  - `scripts/01_inspect_and_extract.py`: Inspeção somente leitura de imagens CD-ROM Mode 2 Form 1 (2.352B) e geração do manifesto `iso_manifest.json` com 392 arquivos indexados.
  - `scripts/02_decode_sprites.py`: Decodificador de sprites 4bpp column-major uncompressed (`flg=0x01`) e SNK-LZ (`flg=0x03`), com leitura de paletas RGB555 no offset `0x3E6960` em `SLES_533.81`.
  - `scripts/03_build_mod_iso.py`: Re-encoder de tiles 16x16, compressor LZ e reconstrutor de setores com cálculo exato de ECMA-130 EDC e Reed-Solomon P/Q ECC.
  - `scripts/04_pcsx2_texture_helper.py`: Helper para substituição dinâmica de texturas em tempo de execução no PCSX2 Qt via hashes hexadecimais.
  - `scripts/05_prepare_custom_frames.py`: Pipeline de ingestão com remoção de fundo magenta (`#FF00FF`), fatiamento conexo de spritesheets e quantização estrita para 16 cores.
  - `scripts/06_build_custom_voices.py`: Conversor de áudio para CRI ADX v4 mono 24kHz e empacotamento em contêineres CRI ACX para `/ADX/BGM.AFS`.
  - `scripts/07_disc_editor.py`: Editor de arquivos offline com validação LBA-safe e recálculo de integridade física.
  - `scripts/08_web_character_studio.py`: Estúdio local em navegador web (porta 8765) com visualização dos 46 personagens, player de animação, edição de paletas A/B/C/D e reprodução de voz.

### Segurança (Security)
- Implementada proteção contra execução arbitrária de código: o agente de inteligência artificial é estritamente limitado ao catálogo de skills determinísticas.
- Garantia de imutabilidade: a ISO original e os arquivos extraídos em `extracted/` são marcados como somente leitura e nunca são abertos em modo de escrita.
- Validação atômica de teto LBA: patches que ultrapassem o número exato de setores alocados pela ISO9660 original são bloqueados antes da escrita.
