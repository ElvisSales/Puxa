"""
Baixador de imagens e vídeos do Instagram e Facebook.
Rode com:  python app.py   e abra http://localhost:5000
"""
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile

from flask import Flask, jsonify, render_template, request, send_file
import yt_dlp

app = Flask(__name__)

# Opcional: arquivo de cookies (formato Netscape) para posts que exigem login
COOKIES_FILE = os.environ.get("COOKIES_FILE") or (
    "cookies.txt" if os.path.exists("cookies.txt") else None
)

IMG_EXT = {".jpg", ".jpeg", ".png", ".webp", ".heic"}
VID_EXT = {".mp4", ".mov", ".webm", ".mkv", ".m4v"}

PADROES = {
    "instagram": re.compile(r"^https?://(www\.)?(instagram\.com|instagr\.am)/", re.I),
    "facebook": re.compile(r"^https?://(www\.|m\.|web\.)?(facebook\.com|fb\.watch|fb\.com)/", re.I),
}


def detectar_plataforma(url):
    for nome, padrao in PADROES.items():
        if padrao.match(url):
            return nome
    return None


def baixar_video(url, pasta):
    tem_ffmpeg = shutil.which("ffmpeg") is not None
    opts = {
        "outtmpl": os.path.join(pasta, "%(id)s_%(autonumber)s.%(ext)s"),
        "quiet": True,
        "noprogress": True,
        "noplaylist": False,
        # Com ffmpeg junta vídeo + áudio na melhor qualidade; sem ele, pega o melhor arquivo único
        "format": "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b" if tem_ffmpeg else "b[ext=mp4]/b",
    }
    if tem_ffmpeg:
        opts["merge_output_format"] = "mp4"
    if COOKIES_FILE:
        opts["cookiefile"] = COOKIES_FILE
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])


def baixar_imagens(url, pasta):
    cmd = [sys.executable, "-m", "gallery_dl", "-D", pasta, "--no-mtime"]
    if COOKIES_FILE:
        cmd += ["--cookies", COOKIES_FILE]
    cmd.append(url)
    resultado = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if resultado.returncode not in (0, 4):  # 4 = alguns itens falharam, mas outros vieram
        raise RuntimeError(resultado.stderr.strip() or "Falha ao baixar as imagens.")


def arquivos_da_pasta(pasta, extensoes):
    achados = []
    for raiz, _, nomes in os.walk(pasta):
        for nome in sorted(nomes):
            if os.path.splitext(nome)[1].lower() in extensoes:
                achados.append(os.path.join(raiz, nome))
    return achados


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/baixar", methods=["POST"])
def baixar():
    dados = request.get_json(silent=True) or {}
    url = (dados.get("url") or "").strip()
    plataforma = dados.get("plataforma")
    tipo = dados.get("tipo")

    if not url:
        return jsonify(erro="Cole um link primeiro."), 400
    if tipo not in ("imagem", "video"):
        return jsonify(erro="Escolha se quer baixar imagem ou vídeo."), 400

    detectada = detectar_plataforma(url)
    if not detectada:
        return jsonify(erro="Esse link não parece ser do Instagram nem do Facebook."), 400
    if plataforma and plataforma != detectada:
        nomes = {"instagram": "Instagram", "facebook": "Facebook"}
        return jsonify(erro=f"Esse link é do {nomes[detectada]}, não do {nomes[plataforma]}."), 400

    pasta = tempfile.mkdtemp(prefix="baixador_")
    try:
        if tipo == "video":
            baixar_video(url, pasta)
            arquivos = arquivos_da_pasta(pasta, VID_EXT)
        else:
            baixar_imagens(url, pasta)
            arquivos = arquivos_da_pasta(pasta, IMG_EXT)

        if not arquivos:
            msg = "Nenhum vídeo encontrado nesse link." if tipo == "video" else "Nenhuma imagem encontrada nesse link."
            return jsonify(erro=msg + " Confira se escolheu o tipo certo ou se o post é público."), 404

        if len(arquivos) == 1:
            caminho = arquivos[0]
            with open(caminho, "rb") as f:
                conteudo = io.BytesIO(f.read())
            return send_file(conteudo, as_attachment=True, download_name=f"{detectada}_{os.path.basename(caminho)}")

        # Vários arquivos (carrossel) → um .zip
        zip_mem = io.BytesIO()
        with zipfile.ZipFile(zip_mem, "w", zipfile.ZIP_DEFLATED) as z:
            for caminho in arquivos:
                z.write(caminho, os.path.basename(caminho))
        zip_mem.seek(0)
        return send_file(zip_mem, as_attachment=True, download_name=f"{detectada}_{tipo}s.zip",
                         mimetype="application/zip")

    except yt_dlp.utils.DownloadError as e:
        texto = str(e)
        if "login" in texto.lower() or "cookies" in texto.lower():
            return jsonify(erro="Esse post exige login. Veja no LEIA-ME como usar o cookies.txt."), 403
        return jsonify(erro="Não consegui baixar: " + texto.replace("ERROR: ", "")[:300]), 502
    except subprocess.TimeoutExpired:
        return jsonify(erro="Demorou demais para baixar. Tente de novo."), 504
    except Exception as e:  # noqa: BLE001
        return jsonify(erro="Não consegui baixar: " + str(e)[:300]), 502
    finally:
        shutil.rmtree(pasta, ignore_errors=True)


if __name__ == "__main__":
    print("Abra no navegador: http://localhost:5000")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
