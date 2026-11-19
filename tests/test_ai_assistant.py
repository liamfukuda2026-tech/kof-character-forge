"""Unit tests for core.ai_assistant (ForgeAIAssistant structured tool caller)."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from core.ai_assistant import ForgeAIAssistant
from core.forge_project import ForgeProject


class TestAIAssistant(unittest.TestCase):
    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp(prefix="test_ai_ws_"))
        self.ai = ForgeAIAssistant(workspace_dir=self.test_dir)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_safety_rejection_of_arbitrary_binary_modifications(self):
        unsafe_prompts = [
            "Modifique o byte 0x3E6960 para FF no executável",
            "Altere bytes arbitrários no offset 0x1234",
            "Patch raw hex directly into memory",
            "Injete assembly payload no executável",
            "Bypass LBA checksum recalculation",
            "Escreva bytes diretamente no SLES_533.81",
        ]

        for p in unsafe_prompts:
            res = self.ai.chat(p)
            self.assertEqual(res["status"], "rejected", f"Failed to reject unsafe prompt: {p}")
            self.assertIn("Ação Bloqueada por Segurança LBA-Safe", res["reply"])
            self.assertIsNone(res["skill_executed"])

    def test_inspect_iso_prompt_interpretation(self):
        prompts = [
            "Inspecione a ISO do jogo",
            "Verifique o disco original",
            "Inspect the ISO image please",
            "Check disc status",
        ]

        for p in prompts:
            res = self.ai.chat(p)
            self.assertEqual(res["status"], "success")
            self.assertEqual(res["skill_executed"], "skill_inspect_iso")
            self.assertIn("Verificação da ISO Concluída", res["reply"])

    def test_create_project_prompt_interpretation(self):
        res = self.ai.chat("Crie um lutador chamado Terry Flamejante baseado no Terry")
        self.assertEqual(res["status"], "success")
        self.assertEqual(res["skill_executed"], "skill_create_project")
        self.assertEqual(res["parameters"]["project_name"], "Terry Flamejante")
        self.assertEqual(res["parameters"]["base_slot"], "Terry Bogard")
        self.assertEqual(self.ai.active_project, "Terry Flamejante")

        # Cleanup created project in workspace
        default_p = ForgeProject.load("Terry Flamejante", workspace_dir=self.test_dir)
        default_p.delete()

    def test_active_project_context_retention(self):
        # Create a test project in temporary workspace
        proj = ForgeProject.create("Context Test", "Terry Bogard", workspace_dir=self.test_dir)
        self.ai.set_active_project("Context Test")
        self.assertEqual(self.ai.active_project, "Context Test")

        # Now ask to validate without naming the project
        res = self.ai.chat("Valide o personagem agora")
        self.assertEqual(res["status"], "success")
        self.assertEqual(res["skill_executed"], "skill_validate_character")

    def test_action_log_recorded(self):
        initial_log_len = len(self.ai.action_log)
        self.ai.chat("Inspecione a ISO")
        self.assertGreater(len(self.ai.action_log), initial_log_len)
        event_types = [entry["type"] for entry in self.ai.action_log]
        self.assertIn("prompt_received", event_types)
        self.assertIn("tool_dispatch", event_types)
        self.assertIn("tool_complete", event_types)

    def test_help_prompt(self):
        res = self.ai.chat("Ajuda, o que você pode fazer?")
        self.assertEqual(res["action"], "help")
        self.assertIn("assistente do KOF 2002 Character Forge", res["reply"])


if __name__ == "__main__":
    unittest.main()
