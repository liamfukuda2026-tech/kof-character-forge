# Guia de Instalação e Configuração — KOF Character Forge AI
**The King of Fighters 2002 (PlayStation 2 — SLES-53381)**  
**Plataforma Host:** Windows 10 / Windows 11 (64-bit)

---

## 1. Pré-requisitos de Sistema

Antes de iniciar a instalação da plataforma, certifique-se de possuir instalado no Windows:

- **Sistema Operacional:** Windows 10 ou Windows 11 (64-bit).
- **Python:** Versão 3.12 ou superior ([python.org](https://www.python.org/downloads/)).  
  > [!IMPORTANT]
  > Durante a instalação do Python, marque obrigatoriamente a opção **"Add Python.exe to PATH"**.
- **Git:** Git for Windows ([git-scm.com](https://git-scm.com/)).
- **PCSX2:** PCSX2 Qt v2.0+ (recomendado: v2.8.2 de 64-bit disponível em [pcsx2.net](https://pcsx2.net/)).
- **FFmpeg (Opcional, para conversão de vozes ADX):** Adicionado ao PATH do Windows ([gyan.dev/ffmpeg](https://www.gyan.dev/ffmpeg/builds/)).

---

## 2. Preparação do Ambiente Virtual (`.venv`)

Abra o **PowerShell** no diretório do projeto (`G:\KOF`) e execute:

```powershell
# 1. Navegue até o diretório do projeto
cd G:\KOF

# 2. Crie o ambiente virtual Python
python -m venv .venv

# 3. Habilite a execução de scripts no PowerShell (se necessário)
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope Process

# 4. Ative o ambiente virtual
.venv\Scripts\Activate.ps1

# 5. Instale as bibliotecas obrigatórias
pip install --upgrade pip
pip install pycdlib Pillow numpy
```

Para confirmar que todas as dependências estão prontas:

```powershell
.venv\Scripts\python.exe -c "import pycdlib, PIL, numpy; print('Dependências instaladas com sucesso!')"
```

---

## 3. Fornecendo a Imagem do Jogo (ISO / BIN)

Por motivos de direitos autorais e conformidade legal, o instalador e o repositório **não distribuem** ROMs, ISOs, BIOS de PS2 ou arquivos proprietários da SNK Playmore. Você deve fornecer sua própria cópia física ou imagem legalmente adquirida:

1. Obtenha a cópia de **The King of Fighters 2002 (PlayStation 2 — Europa, PAL)**.
2. Certifique-se de que a imagem está no formato CD-ROM bruto (`.bin` + `.cue`) ou `.iso`.
3. Copie os arquivos para a raiz do projeto com o nome:
   - `King of Fighters 2002, The (Europe).bin`
   - `King of Fighters 2002, The (Europe).cue`
4. Execute o inspetor de disco para verificar a compatibilidade e gerar o manifesto:
   ```powershell
   .venv\Scripts\python.exe scripts/01_inspect_and_extract.py
   ```
   O script confirmará o serial `SLES_533.81`, a geometria de 2.352 bytes/setor (Mode 2 Form 1), validará a tabela de 392 arquivos e criará o arquivo `scripts/iso_manifest.json`.

---

## 4. Configuração do Emulador PCSX2

Para testar suas modificações diretamente da plataforma:

1. Abra o **PCSX2 Qt** (`pcsx2-qt.exe`).
2. Conclua o assistente inicial de configuração de BIOS e controles.
3. Localize o executável do emulador (por padrão em `C:\Program Files\PCSX2\pcsx2-qt.exe` ou na pasta descompactada).
4. O Web Character Studio localiza automaticamente o PCSX2 em locais padrões do sistema. Caso tenha instalado em pasta personalizada, você pode configurar o caminho na interface ou via flag:
   ```powershell
   .venv\Scripts\python.exe scripts/04_pcsx2_texture_helper.py init --pcsx2-dir "C:\Seu\Caminho\PCSX2"
   ```

---

## 5. Inicialização do Web Character Studio

Para iniciar o estúdio visual e o assistente de IA no navegador:

```powershell
.venv\Scripts\python.exe scripts/08_web_character_studio.py --open
```

O servidor local iniciará na porta `8765` e abrirá automaticamente seu navegador padrão em:
**`http://127.0.0.1:8765/`**

### Funcionalidades Prontas para Uso no Estúdio:
- **Painel de Lutadores:** Visualize todos os 46 lutadores com seus sprites originais decodificados em tempo real.
- **Botão `[ + CRIAR PERSONAGEM ]`:** Inicia o assistente de criação rápida (ex.: crie o "Terry Legendary" a partir de Terry Bogard).
- **Player de Animações:** Controle de FPS, pausa, navegação frame a frame e visualização de ataques especiais.
- **Player de Áudio:** Escute as vozes e gritos dos lutadores gravados no contêiner CRI ADX v4.
- **Editor de Paletas:** Modifique as paletas de botões A, B, C e D com preview instantâneo.
- **Painel Forge AI Assistant:** Converse com o assistente na barra lateral para orquestrar importação e validação de sprites.

---

## 6. Solução de Problemas Frequentes

### Erro: `Port 8765 is already in use`
Se outra instância do estúdio já estiver em execução:
```powershell
.venv\Scripts\python.exe scripts/08_web_character_studio.py --port 8766 --open
```

### Erro: `Unrecognized disc image format`
A imagem fornecida não possui a assinatura ISO9660 padrão (`\x01CD001`). Certifique-se de fornecer a imagem do jogo de PS2 e não uma ROM compactada de arcade (`.zip`).

### Erro: `Rebuilt file exceeds original slot size`
O limite de tamanho LBA do lutador selecionado foi excedido. Utilize o validador (`skill_validate_character`) ou reduza o tamanho da imagem através de recorte das áreas transparentes.
