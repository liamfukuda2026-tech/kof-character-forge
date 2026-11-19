# Diretrizes de Contribuição — KOF Character Forge AI

Agradecemos o seu interesse em contribuir para o **KOF Character Forge AI**! Este projeto é uma iniciativa comunitária aberta dedicada a fornecer ferramentas profissionais, acessíveis e tecnicamente rigorosas de engenharia reversa e modding para *The King of Fighters 2002 (PS2)*.

---

## 1. Princípios e Regras Fundamentais

Antes de escrever qualquer código ou submeter um Pull Request (PR), leia atentamente os pilares inegociáveis do projeto:

1. **Conformidade Legal e Direitos Autorais (Regra Zero):**
   - **NUNCA** faça commit ou envie arquivos de ISO, imagens `.bin`, BIOS de PlayStation 2, ROMs de arcade ou arquivos proprietários extraídos da SNK Playmore.
   - O repositório armazena exclusivamente ferramentas de engenharia reversa, algoritmos matemáticos e código de manipulação de dados. O usuário final deve fornecer sua própria cópia legal do jogo.
2. **Preservação do Projeto Existente:**
   - Não reescreva ferramentas funcionais existentes sem necessidade comprovada.
   - Os scripts de referência (`01` a `08`) devem permanecer retrocompatíveis.
3. **Invariante LBA-Safe:**
   - Nenhuma ferramenta deve tentar expandir o tamanho de arquivos de personagens além dos setores alocados pela ISO original sem reestruturação completa e validada de descritores de volume.
4. **Determinismo nas Modificações:**
   - Todos os conversores binários (sprites, paletas, setores Mode 2) devem produzir saídas bit-a-bit idênticas em condições iguais de entrada.

---

## 2. Fluxo de Trabalho com Git e Branches

Adotamos um modelo de branches estruturado:

- **`main`:** Ramo de produção estável. Cada versão lançada recebe uma tag de release (ex.: `v0.1.0`).
- **`dev`:** Ramo principal de integração em desenvolvimento.
- **`feature/<nome-da-funcionalidade>`:** Branches individuais para novas implementações ou skills.
- **`fix/<nome-do-bug>`:** Branches para correções de defeitos específicos.

### Como Iniciar uma Contribuição:
```powershell
# 1. Crie sua branch a partir de dev
git checkout -b feature/nova-skill-ou-melhoria

# 2. Desenvolva sua modificação respeitando os padrões de estilo
# 3. Execute a suíte de testes
.venv\Scripts\python.exe scripts/08_web_character_studio.py --self-test

# 4. Faça commits pequenos e bem documentados
git commit -m "feat(skills): implementa nova skill deterministica de alinhamento"
```

---

## 3. Padrões de Código Python

- **Versão Alvo:** Python 3.12+ (compatível com Windows PowerShell e Linux).
- **Tipagem Estática:** Utilize Type Hints em todas as assinaturas de funções e métodos (`from __future__ import annotations`).
- **Tratamento de Exceções:** Trate erros de I/O e binários com mensagens contextualizadas; evite blocos `except: pass` vazios.
- **Formatação:** Siga as convenções da **PEP 8** com limite de 100 caracteres por linha.
- **Dependências Mínimas:** Prefira a biblioteca padrão do Python sempre que viável. Adições de dependências externas ao `requirements.txt` requerem justificativa técnica.

---

## 4. Estrutura de Testes e Validação

Qualquer Pull Request que altere a camada de domínio (`core/`) ou scripts existentes deve comprovar que todos os testes passam sem regressão:

1. **Autoteste do Web Studio:**
   ```powershell
   .venv\Scripts\python.exe scripts/08_web_character_studio.py --self-test
   ```
2. **Verificação de Integridade EDC/ECC da ISO:**
   ```powershell
   .venv\Scripts\python.exe scripts/03_build_mod_iso.py --verify-only
   ```
3. **Validação de Texturas do PCSX2:**
   ```powershell
   .venv\Scripts\python.exe scripts/04_pcsx2_texture_helper.py validate
   ```

---

## 5. Checklist para Envio de Pull Request (PR)

Antes de abrir seu PR, confirme:
- [ ] O código não inclui arquivos de mídia ou binários proprietários.
- [ ] A branch foi criada a partir de `dev`.
- [ ] O autoteste `--self-test` passou com sucesso.
- [ ] Novas skills ou recursos foram documentados em `docs/SKILLS.md` ou `docs/ARCHITECTURE.md`.
- [ ] O arquivo `CHANGELOG.md` foi atualizado na seção *Unreleased*.
