#!/usr/bin/env python3
"""KOF 2002 (PS2, SLES-53381) - editor offline de arquivos do disco.

Edita QUALQUER arquivo da imagem (Mode 2 / 2352) e reconstroi o disco com
EDC/ECC recalculados. Original (.bin) e extracted/ nunca sao alterados.

Regras LBA-safe: o arquivo editado precisa ter <= tamanho original (se menor,
preenche com 0x00 ate o tamanho original). Maior que o original e recusado.

Uso:
  07_disc_editor.py            -> abre a GUI
  07_disc_editor.py list [filtro]
  07_disc_editor.py export  /DATA/PL1/P27.BIN saida.bin
  07_disc_editor.py import  /DATA/PL1/P27.BIN arquivo_editado.bin
  07_disc_editor.py patch   /SLES_533.81 0x3E6960 "00 11 22"
  07_disc_editor.py revert  /DATA/PL1/P27.BIN | all
  07_disc_editor.py status
  07_disc_editor.py build   [--out build/kof2002_edit.bin]
  07_disc_editor.py verify  [--out build/kof2002_edit.bin]
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
MANIFEST = SCRIPTS / "iso_manifest.json"
EXTRACTED = ROOT / "extracted"
WORK = ROOT / "workspace" / "files"          # arquivos editados (so os alterados)
DEFAULT_OUT = ROOT / "build" / "kof2002_edit.bin"
SECTOR = 2048
RAW = 2352


def _load_builder():
    spec = importlib.util.spec_from_file_location("kof_build", SCRIPTS / "03_build_mod_iso.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["kof_build"] = mod
    spec.loader.exec_module(mod)
    return mod


B = _load_builder()


# ---------------------------------------------------------------- modelo
class Project:
    def __init__(self) -> None:
        self.manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        self.files = {f["clean_path"].upper(): f for f in self.manifest["files"]}
        self.src_bin = Path(self.manifest["image_path"])
        if not self.src_bin.exists():
            cand = list(ROOT.glob("*.bin"))
            if not cand:
                raise FileNotFoundError("imagem original .bin nao encontrada")
            self.src_bin = cand[0]
        if self.manifest["sector_format"] != "bin2352_mode2":
            raise ValueError("este editor suporta apenas bin2352_mode2")

    def entry(self, path: str) -> dict:
        key = B.norm_iso_path(path).split(";")[0]
        if key not in self.files:
            raise KeyError(f"arquivo nao existe no disco: {path}")
        return self.files[key]

    @staticmethod
    def rel(entry: dict) -> Path:
        return Path(entry["clean_path"].lstrip("/"))

    def original_bytes(self, entry: dict) -> bytes:
        p = EXTRACTED / self.rel(entry)
        if p.exists() and p.stat().st_size == entry["size_bytes"]:
            return p.read_bytes()
        # fallback: le direto do disco original
        out = bytearray()
        with self.src_bin.open("rb") as fp:
            for i in range(entry["lba_sectors"]):
                fp.seek((entry["lba_start"] + i) * RAW + 24)
                out += fp.read(SECTOR)
        return bytes(out[: entry["size_bytes"]])

    def edited_path(self, entry: dict) -> Path:
        return WORK / self.rel(entry)

    def is_modified(self, entry: dict) -> bool:
        return self.edited_path(entry).exists()

    def current_bytes(self, entry: dict) -> bytes:
        p = self.edited_path(entry)
        return p.read_bytes() if p.exists() else self.original_bytes(entry)

    def save_bytes(self, entry: dict, data: bytes) -> str:
        size = entry["size_bytes"]
        if len(data) > size:
            raise ValueError(
                f"{entry['clean_path']}: {len(data)} bytes > original {size}. "
                "Recomprima/reduza o arquivo (LBA-safe nao permite crescer)."
            )
        note = ""
        if len(data) < size:
            note = f" (preenchido com {size - len(data)} bytes 0x00)"
            data = data.ljust(size, b"\x00")
        if data == self.original_bytes(entry):
            self.revert(entry)
            return "identico ao original; override removido"
        p = self.edited_path(entry)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return "salvo" + note

    def patch_bytes(self, entry: dict, offset: int, patch: bytes) -> str:
        cur = bytearray(self.current_bytes(entry))
        if offset < 0 or offset + len(patch) > len(cur):
            raise ValueError("patch fora dos limites do arquivo")
        cur[offset : offset + len(patch)] = patch
        return self.save_bytes(entry, bytes(cur))

    def revert(self, entry: dict) -> None:
        p = self.edited_path(entry)
        if p.exists():
            p.unlink()

    def modified_entries(self) -> list[dict]:
        return [e for e in self.files.values() if self.is_modified(e)]

    # --------------------------------------------------------- build/verify
    def build(self, out: Path = DEFAULT_OUT, log=print) -> Path:
        mods = self.modified_entries()
        if not mods:
            raise RuntimeError("nenhum arquivo modificado; nada a construir")
        out.parent.mkdir(parents=True, exist_ok=True)
        log(f"copiando original -> {out.name} ...")
        B.copy_file_chunked(self.src_bin, out)
        patches = [(e, self.current_bytes(e)) for e in mods]
        log(f"aplicando {len(patches)} arquivo(s) com EDC/ECC ...")
        B.patch_disc_image(out, "bin2352_mode2", patches)
        src_cue = self.src_bin.with_suffix(".cue")
        B.write_cue_for_bin(src_cue if src_cue.exists() else None, out.with_suffix(".cue"), out.name)
        log(f"gerado: {out} + {out.with_suffix('.cue').name}")
        ok = self.verify(out, log=log)
        if not ok:
            raise RuntimeError("verificacao falhou apos o build")
        return out

    def verify(self, out: Path = DEFAULT_OUT, log=print) -> bool:
        if not out.exists():
            log(f"nao existe: {out}")
            return False
        mods = {e["clean_path"] for e in self.modified_entries()}
        bad = 0
        with out.open("rb") as fo, self.src_bin.open("rb") as fs:
            if out.stat().st_size != self.src_bin.stat().st_size:
                log("tamanho do disco difere do original")
                return False
            # 1) arquivos editados: conteudo + EDC/ECC
            for e in self.modified_entries():
                want = self.current_bytes(e).ljust(e["lba_sectors"] * SECTOR, b"\0")
                for i in range(e["lba_sectors"]):
                    fo.seek((e["lba_start"] + i) * RAW)
                    sec = fo.read(RAW)
                    if B.verify_mode2_form1_sector(sec) != (True, True, True):
                        bad += 1
                        log(f"EDC/ECC invalido: {e['clean_path']} setor {i}")
                    if sec[24:2072] != want[i * SECTOR : (i + 1) * SECTOR]:
                        bad += 1
                        log(f"conteudo difere: {e['clean_path']} setor {i}")
            # 2) fora dos arquivos editados o disco tem que ser byte-identico
            touched = set()
            for e in self.modified_entries():
                touched.update(range(e["lba_start"], e["lba_start"] + e["lba_sectors"]))
            total = out.stat().st_size // RAW
            diff = 0
            for lba in range(total):
                if lba in touched:
                    continue
                if lba % 4096 == 0:
                    pass
                fo.seek(lba * RAW)
                fs.seek(lba * RAW)
                if fo.read(RAW) != fs.read(RAW):
                    diff += 1
            if diff:
                bad += diff
                log(f"{diff} setores fora dos editados diferem do original")
        log("VERIFY OK" if bad == 0 else f"VERIFY FALHOU ({bad} problemas)")
        return bad == 0


# ---------------------------------------------------------------- util
def parse_hex(s: str) -> bytes:
    s = re.sub(r"[^0-9a-fA-F]", "", s)
    if len(s) % 2:
        raise ValueError("numero impar de digitos hex")
    return bytes.fromhex(s)


def hexdump(data: bytes, base: int = 0) -> str:
    lines = []
    for i in range(0, len(data), 16):
        c = data[i : i + 16]
        hx = " ".join(f"{b:02X}" for b in c).ljust(47)
        asc = "".join(chr(b) if 32 <= b < 127 else "." for b in c)
        lines.append(f"{base + i:08X}  {hx}  {asc}")
    return "\n".join(lines)


def find_pcsx2(hint: str | None = None) -> Path | None:
    cands = []
    if hint:
        cands.append(Path(hint))
    home = Path.home()
    for base in [home / "OneDrive" / "Documentos" / "PCSX2", home / "Documents" / "PCSX2",
                 Path("C:/Program Files/PCSX2"), Path("C:/Program Files (x86)/PCSX2"),
                 Path("C:/PCSX2"), Path("G:/PCSX2"), Path("D:/PCSX2")]:
        cands.append(base)
    for c in cands:
        if c.is_file():
            return c
        if c.is_dir():
            for pat in ("pcsx2-qt.exe", "pcsx2*.exe"):
                hit = next(c.rglob(pat), None) if c.exists() else None
                if hit:
                    return hit
    return None


# ---------------------------------------------------------------- CLI
def cli(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("list"); s.add_argument("filter", nargs="?", default="")
    s = sub.add_parser("export"); s.add_argument("path"); s.add_argument("dest")
    s = sub.add_parser("import"); s.add_argument("path"); s.add_argument("src")
    s = sub.add_parser("patch"); s.add_argument("path"); s.add_argument("offset"); s.add_argument("hex")
    s = sub.add_parser("revert"); s.add_argument("path")
    sub.add_parser("status")
    for n in ("build", "verify"):
        s = sub.add_parser(n); s.add_argument("--out", default=str(DEFAULT_OUT))
    s = sub.add_parser("launch"); s.add_argument("--pcsx2"); s.add_argument("--out", default=str(DEFAULT_OUT))
    a = ap.parse_args(argv)
    p = Project()

    if a.cmd == "list":
        f = a.filter.upper()
        for k, e in sorted(p.files.items()):
            if f in k:
                print(f"{'*' if p.is_modified(e) else ' '} {e['size_bytes']:>10}  LBA {e['lba_start']:>7}  {k}")
    elif a.cmd == "export":
        d = Path(a.dest); d.parent.mkdir(parents=True, exist_ok=True)
        d.write_bytes(p.current_bytes(p.entry(a.path))); print(f"exportado -> {d}")
    elif a.cmd == "import":
        print(p.save_bytes(p.entry(a.path), Path(a.src).read_bytes()))
    elif a.cmd == "patch":
        print(p.patch_bytes(p.entry(a.path), int(a.offset, 0), parse_hex(a.hex)))
    elif a.cmd == "revert":
        if a.path.lower() == "all":
            for e in p.modified_entries():
                p.revert(e)
            print("tudo revertido")
        else:
            p.revert(p.entry(a.path)); print("revertido")
    elif a.cmd == "status":
        m = p.modified_entries()
        print(f"{len(m)} arquivo(s) modificado(s)")
        for e in m:
            print("  ", e["clean_path"], e["size_bytes"])
    elif a.cmd == "build":
        p.build(Path(a.out))
    elif a.cmd == "verify":
        return 0 if p.verify(Path(a.out)) else 1
    elif a.cmd == "launch":
        exe = find_pcsx2(a.pcsx2)
        if not exe:
            print("PCSX2 nao encontrado; use --pcsx2 <caminho do pcsx2-qt.exe>"); return 1
        subprocess.Popen([str(exe), "-batch", "--", str(a.out)])
        print("lancado:", exe)
    else:
        return gui()
    return 0


# ---------------------------------------------------------------- GUI
def gui() -> int:
    import threading
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    p = Project()
    root = tk.Tk()
    root.title("KOF 2002 - Editor de Disco (offline)")
    root.geometry("1250x760")
    st = {"entry": None, "page": 0}
    PAGE = 4096

    top = ttk.Frame(root); top.pack(fill="x", padx=6, pady=4)
    q = tk.StringVar()
    ttk.Label(top, text="Filtro:").pack(side="left")
    ent = ttk.Entry(top, textvariable=q, width=30); ent.pack(side="left", padx=4)
    only_mod = tk.BooleanVar()
    ttk.Checkbutton(top, text="so modificados", variable=only_mod, command=lambda: refresh()).pack(side="left")
    status = tk.StringVar(value=f"{len(p.files)} arquivos")
    ttk.Label(top, textvariable=status).pack(side="right")

    pan = ttk.PanedWindow(root, orient="horizontal"); pan.pack(fill="both", expand=True, padx=6)
    left = ttk.Frame(pan); pan.add(left, weight=2)
    tv = ttk.Treeview(left, columns=("size", "lba", "mod"), show="tree headings", selectmode="browse")
    tv.heading("#0", text="Arquivo"); tv.heading("size", text="Bytes"); tv.heading("lba", text="LBA"); tv.heading("mod", text="Mod")
    tv.column("#0", width=300); tv.column("size", width=80, anchor="e"); tv.column("lba", width=70, anchor="e"); tv.column("mod", width=40, anchor="center")
    sb = ttk.Scrollbar(left, command=tv.yview); tv.configure(yscrollcommand=sb.set)
    tv.pack(side="left", fill="both", expand=True); sb.pack(side="right", fill="y")

    right = ttk.Frame(pan); pan.add(right, weight=3)
    info = tk.StringVar(value="selecione um arquivo")
    ttk.Label(right, textvariable=info).pack(anchor="w")
    bar = ttk.Frame(right); bar.pack(fill="x", pady=3)
    txt = tk.Text(right, font=("Consolas", 10), wrap="none", height=28)
    txt.pack(fill="both", expand=True)

    def refresh(*_):
        f = q.get().upper()
        tv.delete(*tv.get_children())
        for k, e in sorted(p.files.items()):
            m = p.is_modified(e)
            if f in k and (m or not only_mod.get()):
                tv.insert("", "end", iid=k, text=k, values=(e["size_bytes"], e["lba_start"], "*" if m else ""))
        status.set(f"{len(p.files)} arquivos | {len(p.modified_entries())} modificados")

    def show():
        e = st["entry"]
        if not e:
            return
        data = p.current_bytes(e)
        n = max(1, (len(data) + PAGE - 1) // PAGE)
        st["page"] = max(0, min(st["page"], n - 1))
        off = st["page"] * PAGE
        info.set(f"{e['clean_path']} | {len(data)} B | LBA {e['lba_start']} (+{e['lba_sectors']}) | "
                 f"pagina {st['page']+1}/{n} | {'MODIFICADO' if p.is_modified(e) else 'original'}")
        txt.delete("1.0", "end"); txt.insert("1.0", hexdump(data[off : off + PAGE], off))

    def on_sel(_=None):
        s = tv.selection()
        if s:
            st["entry"] = p.files[s[0]]; st["page"] = 0; show()

    def need():
        if not st["entry"]:
            messagebox.showinfo("Editor", "selecione um arquivo")
        return st["entry"]

    def do_export():
        e = need()
        if e:
            d = filedialog.asksaveasfilename(initialfile=Path(e["clean_path"]).name)
            if d:
                Path(d).write_bytes(p.current_bytes(e)); status.set(f"exportado {d}")

    def do_import():
        e = need()
        if e:
            s = filedialog.askopenfilename()
            if s:
                try:
                    status.set(p.save_bytes(e, Path(s).read_bytes()))
                except Exception as ex:
                    messagebox.showerror("Import", str(ex))
                refresh(); show()

    def do_patch():
        e = need()
        if not e:
            return
        w = tk.Toplevel(root); w.title("Patch de bytes")
        ov, hv = tk.StringVar(value=f"0x{st['page']*PAGE:X}"), tk.StringVar()
        ttk.Label(w, text="Offset (0x..):").grid(row=0, column=0); ttk.Entry(w, textvariable=ov, width=20).grid(row=0, column=1)
        ttk.Label(w, text="Bytes hex:").grid(row=1, column=0); ttk.Entry(w, textvariable=hv, width=60).grid(row=1, column=1)
        def ok():
            try:
                status.set(p.patch_bytes(e, int(ov.get(), 0), parse_hex(hv.get())))
                w.destroy(); refresh(); show()
            except Exception as ex:
                messagebox.showerror("Patch", str(ex), parent=w)
        ttk.Button(w, text="Aplicar", command=ok).grid(row=2, column=1, sticky="e")

    def do_search():
        e = need()
        if not e:
            return
        w = tk.Toplevel(root); w.title("Buscar hex")
        hv = tk.StringVar(); ttk.Entry(w, textvariable=hv, width=50).pack(padx=6, pady=6)
        def go():
            try:
                pat = parse_hex(hv.get()); data = p.current_bytes(e)
                start = st["page"] * PAGE + 1
                i = data.find(pat, start)
                if i < 0:
                    i = data.find(pat)
                if i < 0:
                    messagebox.showinfo("Buscar", "nao encontrado", parent=w); return
                st["page"] = i // PAGE; show(); status.set(f"achado em 0x{i:X}")
            except Exception as ex:
                messagebox.showerror("Buscar", str(ex), parent=w)
        ttk.Button(w, text="Proximo", command=go).pack(pady=4)

    def do_revert():
        e = need()
        if e:
            p.revert(e); refresh(); show()

    def page(d):
        st["page"] += d; show()

    def bg(fn):
        def run():
            try:
                fn(lambda m: root.after(0, status.set, m))
                root.after(0, lambda: messagebox.showinfo("Editor", "concluido"))
            except Exception as ex:
                root.after(0, lambda: messagebox.showerror("Editor", str(ex)))
        threading.Thread(target=run, daemon=True).start()

    def do_build():
        bg(lambda log: p.build(DEFAULT_OUT, log=log))

    def do_launch():
        exe = find_pcsx2()
        if not exe:
            f = filedialog.askopenfilename(title="Selecione o pcsx2-qt.exe", filetypes=[("exe", "*.exe")])
            if not f:
                return
            exe = Path(f)
        if not DEFAULT_OUT.exists():
            messagebox.showinfo("Editor", "faca o Build primeiro"); return
        subprocess.Popen([str(exe), "-batch", "--", str(DEFAULT_OUT)])

    for t, c in [("Exportar", do_export), ("Importar", do_import), ("Patch bytes", do_patch), ("Buscar", do_search),
                 ("Reverter", do_revert), ("< Pag", lambda: page(-1)), ("Pag >", lambda: page(1)),
                 ("BUILD ISO", do_build), ("Abrir no PCSX2", do_launch)]:
        ttk.Button(bar, text=t, command=c).pack(side="left", padx=2)

    tv.bind("<<TreeviewSelect>>", on_sel)
    q.trace_add("write", refresh)
    refresh()
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(cli(sys.argv[1:]))
