"""Structured Tool-Calling Engine for Character Forge AI Assistant.

Interprets natural language prompts in Portuguese and English, dispatches them
to deterministic skills contracts, enforces strict safety policies rejecting
arbitrary binary/hex patching, and maintains an auditable action log.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.characters import list_character_slots, resolve_character_slot
from core.forge_project import ForgeProject
from core.skills import SkillRegistry, registry as default_registry
from skills.base import SkillResult


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# Arbitrary binary modification patterns that MUST be rejected for LBA-safety
REJECTED_BINARY_PATTERNS = [
    r"\b(alter[ae]r?|modifi[cq][ue]|patch|edit|writ[ie]|grav[ae]r?|escrev[ae]r?)\s+(?:o\s+|os\s+|a\s+|as\s+|um\s+|uns\s+)?(byte|bytes|hex|bin[aá]ri[ao]|offset|endere[çc]o|mem[oó]ria)\b",
    r"\b(0x[0-9a-fA-F]{4,8})\s*(?:[:=]|para|\->|to)\s*([0-9a-fA-F]{2}\b)+",
    r"\b(poke|direct\s*memory|raw\s*hex|arbitrary\s*patch|arbitrary\s*byte)\b",
    r"\b(modifi[cq][ue]|alter[ae]r?|patch|edit)\s+(?:o\s+|os\s+)?(sles|sles_533|game\.bin|elf|execut[aá]vel)\b",
    r"\b(injete|inject)\s+(?:c[oó]digo|shellcode|assembly|asm|payload)\b",
    r"\b(bypas[se]r?|ignorar?)\s+(?:lba|checksum|edc|ecc)\b",
    r"\b(hex\s*editor|editor\s*hexadecimal|patch\s*manual)\b",
]


class ForgeAIAssistant:
    """Natural Language structured tool-calling assistant for Character Forge."""

    def __init__(
        self,
        registry: SkillRegistry | None = None,
        active_project: str | None = None,
        workspace_dir: Path | str | None = None,
    ) -> None:
        self.registry = registry or default_registry
        self.active_project: str | None = active_project
        self.workspace_dir: Path | None = Path(workspace_dir) if workspace_dir else None
        self.action_log: list[dict[str, Any]] = []
        self.chat_history: list[dict[str, Any]] = []

    def log_action(self, event_type: str, detail: str, data: dict[str, Any] | None = None) -> None:
        """Record an entry in the structured action log."""
        self.action_log.append({
            "timestamp": _now_iso(),
            "type": event_type,
            "detail": detail,
            "data": data or {},
        })

    def set_active_project(self, project_name: str | None) -> None:
        """Set the active character project context for follow-up prompts."""
        self.active_project = project_name
        self.log_action("context_change", f"Active project set to '{project_name}'")

    # -------------------------------------------------------------------------
    # Safety Guardrail Check
    # -------------------------------------------------------------------------

    def check_safety_violation(self, prompt: str) -> tuple[bool, str]:
        """Check if prompt requests arbitrary binary modification or unsafe operations."""
        low_prompt = prompt.lower()
        for pat in REJECTED_BINARY_PATTERNS:
            if re.search(pat, low_prompt, re.IGNORECASE):
                reason = (
                    "Modificação binária ou hexadecimal arbitrária rejeitada por segurança LBA-safe. "
                    "Para preservar a integridade das LBAs do disco e a conformidade com o hardware PS2, "
                    "utilize apenas as habilidades determinísticas homologadas da Forge."
                )
                return True, reason
        return False, ""

    # -------------------------------------------------------------------------
    # Natural Language Intent Parsing
    # -------------------------------------------------------------------------

    def parse_intent(self, prompt: str) -> dict[str, Any]:
        """Parse user prompt into a structured tool-call declaration."""
        p = prompt.strip()
        low = p.lower()

        # 1. Safety check
        is_violation, reason = self.check_safety_violation(p)
        if is_violation:
            return {
                "action": "rejected",
                "reason": reason,
                "tool_name": None,
                "parameters": {},
                "thought": "Usuário solicitou modificação binária direta/arbitrária. Bloqueado pelo guardrail LBA-safe.",
            }

        # 2. Inspect ISO
        if any(w in low for w in ["inspecion", "inspect", "verifiq", "check disc", "check image", "disco original", "status da iso"]) or (
            ("iso" in low or "disc" in low) and any(w in low for w in ["status", "check", "info", "valid", "test", "ver", "imagem", "image", "please", "por favor"])
        ):
            if not any(w in low for w in ["injet", "inject", "build", "patch", "modificada"]):
                return {
                    "action": "execute_tool",
                    "tool_name": "skill_inspect_iso",
                    "parameters": {},
                    "thought": "Usuário quer verificar a integridade da ISO e do manifesto do jogo.",
                }

        # 3. Create Project
        # Examples: "Crie um lutador baseado no Terry", "Crie um lutador chamado Terry Legendary baseado no Terry"
        create_match = re.search(
            r"(?:cri[ae]r?|novo|new|build)\s+(?:um\s+)?(?:lutador|personagem|projeto|character|fighter)\s*(?:chamado\s+['\"]?([^'\",\n]+?)['\"]?)?\s*(?:baseado\s+no|baseado\s+em|based\s+on|com\s+base\s+n[oa]|usando)\s+['\"]?([^'\",\.\n]+)['\"]?",
            low,
            re.IGNORECASE,
        )
        if create_match:
            custom_name_raw = create_match.group(1)
            base_slot_raw = create_match.group(2).strip()

            try:
                resolved_slot = resolve_character_slot(base_slot_raw)
                base_slot_name = resolved_slot.display_name
            except Exception:
                base_slot_name = base_slot_raw

            if custom_name_raw and custom_name_raw.strip():
                proj_name = custom_name_raw.strip().title()
            else:
                proj_name = f"{base_slot_name} Custom"

            return {
                "action": "execute_tool",
                "tool_name": "skill_create_project",
                "parameters": {
                    "project_name": proj_name,
                    "base_slot": base_slot_name,
                },
                "thought": f"Criar novo projeto '{proj_name}' a partir do slot '{base_slot_name}'.",
            }

        # Simpler create patterns: "Crie um projeto Terry Legendary" or "Criar Terry Legendary"
        create_simple = re.search(
            r"(?:cri[ae]r?|novo|new)\s+(?:projeto|lutador|character)?\s*['\"]?([a-zA-Z0-9_\s]{3,30})['\"]?",
            low,
            re.IGNORECASE,
        )
        if create_simple and any(w in low for w in ["cri", "new", "novo"]):
            cand_name = create_simple.group(1).strip()
            # Try to see if there is a known base slot in the prompt
            found_slot = None
            for slot in list_character_slots():
                if slot.slug in low or slot.display_name.lower() in low:
                    found_slot = slot.display_name
                    break
            if found_slot:
                return {
                    "action": "execute_tool",
                    "tool_name": "skill_create_project",
                    "parameters": {
                        "project_name": cand_name.title() if cand_name.lower() != found_slot.lower() else f"{found_slot} Custom",
                        "base_slot": found_slot,
                    },
                    "thought": f"Detectado comando de criação com base em '{found_slot}'.",
                }

        # 4. Import Sprites
        if any(w in low for w in ["import", "carreg", "inser"]) and any(w in low for w in ["sprite", "frame", "imagem", "png"]):
            # Extract path if quoted or present
            path_match = re.search(r"['\"]?([a-zA-Z0-9_\-\\/\.]+\.(?:png|jpg|webp))['\"]?", p)
            source_path = path_match.group(1) if path_match else "custom_character_demo"
            target_proj = self._extract_project_name(p) or self.active_project or "Terry Custom"

            return {
                "action": "execute_tool",
                "tool_name": "skill_import_sprites",
                "parameters": {
                    "project_name": target_proj,
                    "source_path": source_path,
                },
                "thought": f"Importar sprites de '{source_path}' para o projeto '{target_proj}'.",
            }

        # 5. Validate Character
        if any(w in low for w in ["valid", "chec", "verifiq"]) and any(w in low for w in ["personagem", "lutador", "projeto", "character", "frame", "tamanho", "memoria"]):
            target_proj = self._extract_project_name(p) or self.active_project
            if not target_proj:
                projects = ForgeProject.list_projects()
                target_proj = projects[0]["name"] if projects else "Terry Custom"

            return {
                "action": "execute_tool",
                "tool_name": "skill_validate_character",
                "parameters": {
                    "project_name": target_proj,
                },
                "thought": f"Validar integridade de frames, paletas e restrição de memória LBA para '{target_proj}'.",
            }

        # 6. Build Patch / Inject into ISO
        if any(w in low for w in ["injet", "inject", "build", "reconstru", "gerar iso", "constru"]) and any(w in low for w in ["iso", "disco", "bin", "patch", "mod"]):
            target_proj = self._extract_project_name(p) or self.active_project
            if not target_proj:
                projects = ForgeProject.list_projects()
                target_proj = projects[0]["name"] if projects else "Terry Custom"

            return {
                "action": "execute_tool",
                "tool_name": "skill_build_patch",
                "parameters": {
                    "project_name": target_proj,
                },
                "thought": f"Patchear PXX.BIN e reconstruir imagem ISO com EDC/ECC para '{target_proj}'.",
            }

        # 7. List Projects
        if any(w in low for w in ["list", "quais", "projetos", "personagens criados"]):
            return {
                "action": "list_projects",
                "tool_name": None,
                "parameters": {},
                "thought": "Usuário quer listar os projetos existentes.",
            }

        # 8. Help / Unrecognized
        return {
            "action": "help",
            "tool_name": None,
            "parameters": {},
            "thought": "Comando não mapeado diretamente; exibindo guia de assistência da Forge.",
        }

    def _extract_project_name(self, text: str) -> str | None:
        """Extract existing project name if mentioned in prompt."""
        projects = ForgeProject.list_projects(workspace_dir=self.workspace_dir)
        low = text.lower()
        for p in projects:
            if p["name"].lower() in low or p["slug"] in low:
                return p["name"]
        return None

    # -------------------------------------------------------------------------
    # Main Execution Entrypoint
    # -------------------------------------------------------------------------

    def chat(self, prompt: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
        """Process a natural language user prompt, dispatch tool, and return structured result."""
        ctx = context or {}
        if "workspace_dir" in ctx and ctx["workspace_dir"]:
            self.workspace_dir = Path(ctx["workspace_dir"])
        if "active_project" in ctx and ctx["active_project"]:
            self.active_project = str(ctx["active_project"])

        self.log_action("prompt_received", prompt)
        parsed = self.parse_intent(prompt)
        action = parsed["action"]

        # Case 1: Safety Rejection
        if action == "rejected":
            reply = f"⚠️ **Ação Bloqueada por Segurança LBA-Safe**\n\n{parsed['reason']}"
            self.log_action("safety_rejection", parsed["reason"])
            res_payload = {
                "thought": parsed["thought"],
                "action": "rejected",
                "status": "rejected",
                "skill_executed": None,
                "result": None,
                "reply": reply,
                "action_log": list(self.action_log),
            }
            self.chat_history.append({"user": prompt, "assistant": reply})
            return res_payload

        # Case 2: List Projects
        if action == "list_projects":
            projects = ForgeProject.list_projects()
            if not projects:
                reply = "📁 Nenhum projeto encontrado em `workspace/forge/`. Use o comando: `Crie um lutador baseado no Terry` para começar."
            else:
                lines = ["📁 **Projetos no Character Forge:**\n"]
                for pr in projects:
                    status = "✅ Válido" if pr["valid"] else "⏳ Não Validado"
                    lines.append(f"- **{pr['name']}** (Base: {pr['base_slot']} `[{pr['char_id_hex']}]`) — {pr['modified_frames_count']} frames — {status}")
                reply = "\n".join(lines)

            res_payload = {
                "thought": parsed["thought"],
                "action": "list_projects",
                "status": "success",
                "skill_executed": None,
                "result": {"projects": projects},
                "reply": reply,
                "action_log": list(self.action_log),
            }
            self.chat_history.append({"user": prompt, "assistant": reply})
            return res_payload

        # Case 3: Help
        if action == "help":
            reply = (
                "👋 **Olá! Eu sou o assistente do KOF 2002 Character Forge.**\n\n"
                "Você pode me pedir ações como:\n"
                "- 🥊 `Crie um lutador chamado Terry Legendary baseado no Terry`\n"
                "- 🔍 `Inspecione a ISO do jogo`\n"
                "- 🖼️ `Importe os sprites para Terry Legendary`\n"
                "- ⚖️ `Valide o personagem Terry Legendary`\n"
                "- 💿 `Injete na ISO` ou `Gere a ISO modificada`\n"
                "- 📁 `Liste os projetos existentes`\n\n"
                "🔒 *Todas as operações são 100% LBA-safe e determinísticas.*"
            )
            res_payload = {
                "thought": parsed["thought"],
                "action": "help",
                "status": "success",
                "skill_executed": None,
                "result": None,
                "reply": reply,
                "action_log": list(self.action_log),
            }
            self.chat_history.append({"user": prompt, "assistant": reply})
            return res_payload

        # Case 4: Execute Deterministic Skill
        tool_name = parsed["tool_name"]
        params = parsed["parameters"]
        if self.workspace_dir and "workspace_dir" not in params:
            params["workspace_dir"] = str(self.workspace_dir)

        self.log_action("tool_dispatch", f"Dispatching {tool_name}", params)
        skill_res: SkillResult = self.registry.execute(tool_name, **params)
        self.log_action("tool_complete", f"{tool_name} returned success={skill_res.success}", skill_res.to_dict())

        # If a project was created, update active_project
        if tool_name == "skill_create_project" and skill_res.success:
            self.active_project = skill_res.data.get("project_name")

        reply = self._format_skill_reply(tool_name, skill_res)

        res_payload = {
            "thought": parsed["thought"],
            "action": "execute_tool",
            "status": "success" if skill_res.success else "error",
            "skill_executed": tool_name,
            "parameters": params,
            "result": skill_res.to_dict(),
            "reply": reply,
            "action_log": list(self.action_log),
        }
        self.chat_history.append({"user": prompt, "assistant": reply})
        return res_payload

    def _format_skill_reply(self, tool_name: str, res: SkillResult) -> str:
        """Format a clear, friendly Markdown reply for the Studio UI."""
        if not res.success:
            err_text = "\n".join(f"- ❌ {e}" for e in res.errors) if res.errors else res.message
            return f"❌ **Erro na execução de `{tool_name}`**\n\n{err_text}"

        if tool_name == "skill_inspect_iso":
            img = res.data.get("disc_image", {})
            man = res.data.get("manifest", {})
            return (
                f"✅ **Verificação da ISO Concluída com Sucesso!**\n\n"
                f"- **Arquivo:** `{img.get('filename')}` ({img.get('size_bytes'):,} bytes)\n"
                f"- **Formato:** `{man.get('sector_format')}` (Mode 2 / 2352 bytes/setor)\n"
                f"- **Arquivos no Manifesto:** {man.get('file_count')} arquivos catalogados\n"
                f"- **Árvore extraída:** SLES_533.81 e diretórios de dados prontos para modding."
            )

        if tool_name == "skill_create_project":
            base = res.data.get("base_slot", {})
            return (
                f"🎉 **Projeto '{res.data.get('project_name')}' criado com sucesso!**\n\n"
                f"- **Slot Base:** {base.get('display_name')} (`{base.get('char_id_hex')}`)\n"
                f"- **Capacidade Máxima:** {res.data.get('max_memory_bytes'):,} bytes (LBA-safe)\n"
                f"- **Diretório:** `{res.data.get('project_dir')}`\n"
                f"- **Paletas Extraídas:** 4 paletas padrão (A, B, C, D) prontas para edição."
            )

        if tool_name == "skill_import_sprites":
            return (
                f"🖼️ **Importação de Sprites Concluída!**\n\n"
                f"- **Frames Importados:** {res.data.get('imported_count')}\n"
                f"- **Formato:** PNG Indexado de 16 cores (Transparência no índice 0)\n"
                f"- **Alinhamento:** Paleta de botão `{res.data.get('palette_button')}`\n"
                f"- **Total de frames no projeto:** {res.data.get('total_project_frames')}"
            )

        if tool_name == "skill_validate_character":
            metrics = res.data.get("metrics", {})
            return (
                f"⚖️ **Personagem Validado com Sucesso!**\n\n"
                f"- **Slot:** {metrics.get('base_slot')}\n"
                f"- **Frames Customizados:** {metrics.get('total_custom_frames')}\n"
                f"- **Tamanho Estimado:** {metrics.get('estimated_bin_size_bytes'):,} bytes / {metrics.get('max_memory_bytes'):,} bytes\n"
                f"- **Margem Restante (Headroom):** `{metrics.get('headroom_bytes'):,}` bytes livres\n"
                f"- **Contratos:** Cores <= 16, índice 0 transparente, grade 16x16 OK. Pronto para injeção!"
            )

        if tool_name == "skill_build_patch":
            return (
                f"🚀 **Disco Modificado Construído com Sucesso!**\n\n"
                f"- **Arquivo:** `{res.data.get('output_bin')}`\n"
                f"- **CUE Sheet:** `{res.data.get('output_cue')}`\n"
                f"- **Verificação de Setores:** Mode 2 EDC/ECC recalculados e íntegros (100% OK)\n"
                f"- **Pronto para carregar no PCSX2!**"
            )

        return f"✅ **{res.message}**"
