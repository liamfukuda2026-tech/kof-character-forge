# Protocolo do Forge AI Assistant — KOF Character Forge AI
**Diretrizes de Inteligência Artificial, Chamadas Estruturadas e Sandboxing**  
**Data:** 19 de Novembro de 2026 | **Versão:** 1.0.0

---

## 1. Visão Geral e Filosofia

O **Forge AI Assistant** é o motor cognitivo integrado ao painel lateral do **Web Character Studio** (`08_web_character_studio.py`). Ele foi concebido para democratizar o modding de *The King of Fighters 2002 (PS2)*, permitindo que usuários iniciantes e avançados executem fluxos complexos de engenharia reversa por meio de linguagem natural.

```text
                               ┌────────────────────────────────┐
                               │     Usuário (Chat / UI)        │
                               └────────────────┬───────────────┘
                                                │ Prompt em Linguagem Natural
                                                ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│                            FORGE AI ASSISTANT                                │
│                       (core/ai_assistant.py)                                 │
│  1. Parser Semântico: Extrai intenção e identifica personagem ativo          │
│  2. Mapeamento Cognitivo: Consulta tools_schema (Skills Registradas)         │
│  3. Geração de Tool Call Estruturado (JSON strongly-typed)                   │
└───────────────────────────────────────┬──────────────────────────────────────┘
                                        │ Dispatcher de Tool Calls
                                        ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│                         CAMADA DE EXECUÇÃO DETERMINÍSTICA                    │
│                         (core/skills.py & skills/)                           │
│  - skill_inspect_iso       - skill_validate_character                        │
│  - skill_create_project    - skill_build_patch                               │
│  - skill_import_sprites    - skill_associate_audio                           │
└───────────────────────────────────────┬──────────────────────────────────────┘
                                        │ Operações Validadas
                                        ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│                        SANDBOX DO SISTEMA DE ARQUIVOS                        │
│  - workspace/forge/<projeto>/ (Modificações isoladas)                        │
│  - build/ (Imagens geradas com EDC/ECC)                                      │
│  - extracted/ & Original .bin (Estritamente SOMENTE LEITURA)                 │
└──────────────────────────────────────────────────────────────────────────────┘
```

> [!CAUTION]
> **Regra Fundamental de Segurança:** O modelo de IA **nunca** gera bytes binários diretamente e **nunca** possui acesso a interpretadores de linha de comando (`cmd`, `powershell`, `bash`, `subprocess.Popen` aberto). Todas as modificações passam exclusivamente pelas ferramentas determinísticas registradas com parâmetros tipados e validados.

---

## 2. Protocolo de Comunicação e Ciclo de Vida

A interface Web comunica-se com o assistente através do endpoint:
`POST /api/forge/ai/chat`

### Formato do Payload de Requisição
```json
{
  "project_name": "Terry Legendary",
  "active_char_id": 3,
  "message": "Importe o frame 0000 e verifique se o personagem continua compatível com a ISO.",
  "history": [
    {"role": "user", "content": "Crie um lutador baseado no Terry."},
    {"role": "assistant", "content": "Projeto 'Terry Legendary' criado com base no Terry Bogard (P03)."}
  ]
}
```

### Ciclo de Processamento do Agente
1. **Resolução de Contexto:** Identifica o projeto ativo em `workspace/forge/<project_name>/` e lê o manifesto `character.json`.
2. **Reconhecimento de Intenção:** Identifica se o usuário deseja criar um projeto, importar assets, inspecionar erros, validar limites ou preparar a imagem para jogo.
3. **Despacho Estruturado (Tool Calling):** Emite uma ou mais chamadas a skills determinísticas com argumentos validados.
4. **Execução das Skills e Coleta de Resultados:** O motor em `core/skills.py` executa as funções Python associadas, verificando pré-requisitos e limites.
5. **Formulação de Resposta Explicativa:** O assistente sintetiza o resultado técnico em uma mensagem clara e concisa no chat, destacando alertas e próximos passos.

---

## 3. Esquemas de Chamada de Ferramentas (Tool Schemas)

O assistente possui um catálogo fixo de funções que pode invocar:

### 3.1. `skill_create_project`
```json
{
  "name": "skill_create_project",
  "description": "Cria um novo projeto isolado de personagem a partir de um slot existente.",
  "parameters": {
    "type": "object",
    "properties": {
      "project_name": {"type": "string", "description": "Nome do novo personagem"},
      "base_char_id": {"type": "integer", "description": "ID canônico do personagem base (ex: 3 para Terry)"},
      "author": {"type": "string", "description": "Nome do autor ou criador"}
    },
    "required": ["project_name", "base_char_id"]
  }
}
```

### 3.2. `skill_import_sprites`
```json
{
  "name": "skill_import_sprites",
  "description": "Importa e quantiza um PNG/JPG/WEBP para paleta indexada de 16 cores.",
  "parameters": {
    "type": "object",
    "properties": {
      "project_name": {"type": "string"},
      "frame_index": {"type": "integer", "description": "Índice do frame (0..N)"},
      "source_image_path": {"type": "string", "description": "Caminho do arquivo de imagem a importar"},
      "chroma_key": {"type": "string", "default": "#FF00FF"}
    },
    "required": ["project_name", "frame_index", "source_image_path"]
  }
}
```

### 3.3. `skill_validate_character`
```json
{
  "name": "skill_validate_character",
  "description": "Executa auditoria completa de conformidade LBA e paleta do projeto.",
  "parameters": {
    "type": "object",
    "properties": {
      "project_name": {"type": "string"},
      "strict_lba": {"type": "boolean", "default": true}
    },
    "required": ["project_name"]
  }
}
```

### 3.4. `skill_build_patch`
```json
{
  "name": "skill_build_patch",
  "description": "Re-codifica os sprites e paletas e aplica o patch LBA-safe na ISO com recálculo EDC/ECC.",
  "parameters": {
    "type": "object",
    "properties": {
      "project_name": {"type": "string"},
      "output_image": {"type": "string", "default": "G:/KOF/build/kof2002_mod.bin"},
      "update_palette": {"type": "boolean", "default": true}
    },
    "required": ["project_name"]
  }
}
```

---

## 4. Exemplos Reais de Interação

### Exemplo 1: Criação de Personagem por Linguagem Natural
**Usuário:**  
> *"Crie um novo lutador baseado no Terry Bogard chamado 'Terry Legendary'. Quero manter a jogabilidade original dele mas preparar para novos sprites."*

**Ação do Forge AI:**
1. Mapeia a intenção para a skill `skill_create_project`.
2. Identifica que Terry Bogard possui $cid = 0x03$.
3. Executa a chamada:
   ```json
   {
     "tool": "skill_create_project",
     "arguments": {
       "project_name": "Terry Legendary",
       "base_char_id": 3,
       "author": "Usuário"
     }
   }
   ```
4. A skill cria `workspace/forge/terry_legendary/` com `character.json` registrando o teto de 506 setores (1.034.776 bytes).
5. **Resposta do Assistente:**  
   *"Projeto **Terry Legendary** criado com sucesso baseado no Terry Bogard (P03)! O projeto está alocado para o slot LBA 30.996 com capacidade máxima de 1.034.776 bytes (506 setores). Você já pode importar os novos sprites no painel de frames."*

---

### Exemplo 2: Bloqueio Preventivo por Violação de Limites LBA
**Usuário:**  
> *"Injete o Terry Legendary na ISO para eu testar agora."*

**Ação do Forge AI:**
1. O assistente não executa `skill_build_patch` imediatamente. Primeiro invoca `skill_validate_character`:
   ```json
   {
     "tool": "skill_validate_character",
     "arguments": {"project_name": "Terry Legendary"}
   }
   ```
2. O validador detecta que o frame 0012 foi importado em alta resolução sem compressão adequada e o payload projetado totaliza 1.050.200 bytes (excedendo os 1.034.776 bytes permitidos).
3. O validador retorna status `INCOMPATÍVEL`:
   ```json
   {
     "status": "INCOMPATÍVEL",
     "errors": ["Payload projetado (1.050.200 B) excede o teto LBA do slot (1.034.776 B) em 15.424 bytes."]
   }
   ```
4. **Resposta do Assistente:**  
   *"A gravação na ISO foi **bloqueada com segurança**. O tamanho atual dos sprites do Terry Legendary excede a capacidade do slot original em 15.424 bytes. Se gravássemos agora, os arquivos do lutador seguinte (Andy Bogard em P04.BIN) seriam corrompidos. Recomendo recortar áreas vazias no frame 0012 ou utilizar compressão LZ para reduzir o payload antes de gravar."*

---

## 5. Diretrizes de Segurança e Sandboxing

1. **Imutabilidade Estrita dos Arquivos-Fonte:** A imagem de disco original (`King of Fighters 2002, The (Europe).bin`) e o diretório de extração (`extracted/`) nunca são abertos em modo de escrita por nenhuma skill disparada pelo assistente.
2. **Confirmação Obrigatória para Builds:** Operações que geram arquivos pesados em disco (como `skill_build_patch`, que gera imagens `.bin` de 446 MB em `build/`) sempre solicitam confirmação ativa do usuário na interface antes de prosseguir.
3. **Gestão Segura de Chaves de API:** Chaves para provedores de LLM externos (Google Gemini, Anthropic Claude, OpenAI) são mantidas em variáveis de ambiente locais ou no armazenamento do navegador (`localStorage`), nunca sendo escritas em arquivos de configuração compartilhados ou versionados no Git.
4. **Isolamento de Erros:** Exceções levantadas por bibliotecas subjacentes (Pillow, pycdlib, struct) são capturadas pelo dispatcher de skills e transformadas em relatórios legíveis, impedindo o encerramento inesperado do servidor web.
