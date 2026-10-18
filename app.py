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
from html import unescape

import requests

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
        "socket_timeout": 20,
        "retries": 2,
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
    cmd = [sys.executable, "-m", "gallery_dl", "-D", pasta, "--no-mtime",
           "-o", "extractor.timeout=20", "-o", "downloader.http.timeout=20", "-R", "2"]
    if COOKIES_FILE:
        cmd += ["--cookies", COOKIES_FILE]
    cmd.append(url)
    resultado = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
    if resultado.returncode not in (0, 4):  # 4 = alguns itens falharam, mas outros vieram
        raise RuntimeError(resultado.stderr.strip() or "Falha ao baixar as imagens.")


NAVEGADOR = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/128.0 Safari/537.36",
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
}


def _limpar_url(u):
    u = unescape(u)
    return u.replace("\\/", "/").replace("\\u0026", "&").replace("\\", "")


def urls_imagem_instagram(url):
    """Plano B: lê a página pública de incorporação (embed) do post."""
    m = re.search(r"instagram\.com/(?:[\w.]+/)?(p|reel|reels|tv)/([\w-]+)", url)
    if not m:
        return []
    embed = f"https://www.instagram.com/p/{m.group(2)}/embed/captioned/"
    html = requests.get(embed, headers=NAVEGADOR, timeout=20).text
    achadas = []
    # Carrossel e posts com JSON embutido
    for bruto in re.findall(r'display_url\\?":\\?"(.*?)\\?"', html):
        achadas.append(_limpar_url(bruto))
    # Imagem principal do embed (pega a maior do srcset)
    for srcset in re.findall(r'class="EmbeddedMediaImage"[^>]*srcset="([^"]+)"', html):
        achadas.append(_limpar_url(srcset.split(",")[-1].strip().split(" ")[0]))
    for src in re.findall(r'class="EmbeddedMediaImage"[^>]*src="([^"]+)"', html):
        achadas.append(_limpar_url(src))
    vistos, unicas = set(), []
    for a in achadas:
        chave = a.split("?")[0].rsplit("/", 1)[-1]  # mesmo arquivo em tamanhos diferentes
        if a.startswith("http") and chave not in vistos:
            vistos.add(chave)
            unicas.append(a)
    return unicas[:20]


def urls_imagem_og(url):
    """Plano B genérico (Facebook): a imagem de pré-visualização da página."""
    html = requests.get(url, headers=NAVEGADOR, timeout=20).text
    m = re.search(r'<meta[^>]+property="og:image"[^>]+content="([^"]+)"', html) or \
        re.search(r'<meta[^>]+content="([^"]+)"[^>]+property="og:image"', html)
    return [_limpar_url(m.group(1))] if m else []


def urls_imagem_ytdlp(url):
    """Usa o yt-dlp (que já passa pelo Instagram, como nos vídeos) só para ler as fotos do post."""
    opts = {"quiet": True, "no_warnings": True, "skip_download": True,
            "ignore_no_formats_error": True, "socket_timeout": 20}
    if COOKIES_FILE:
        opts["cookiefile"] = COOKIES_FILE
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    itens = list(info.get("entries") or []) or [info]
    urls = []
    for item in itens:
        if not item:
            continue
        formatos = item.get("formats") or []
        if any(f.get("vcodec") not in (None, "none") for f in formatos):
            continue  # é vídeo: pula, aqui só queremos fotos
        miniaturas = [t for t in (item.get("thumbnails") or []) if t.get("url")]
        if miniaturas:
            maior = max(miniaturas, key=lambda t: ((t.get("width") or 0) * (t.get("height") or 0), t.get("preference") or 0))
            urls.append(maior["url"])
        elif item.get("thumbnail"):
            urls.append(item["thumbnail"])
    return urls


def salvar_imagens(urls, pasta, prefixo="imagem"):
    for i, u in enumerate(urls, 1):
        r = requests.get(u, headers=NAVEGADOR, timeout=30)
        tipo = r.headers.get("Content-Type", "")
        if r.ok and tipo.startswith("image/"):
            ext = ".png" if "png" in tipo else ".webp" if "webp" in tipo else ".jpg"
            with open(os.path.join(pasta, f"{prefixo}_{i:02d}{ext}"), "wb") as f:
                f.write(r.content)


def baixar_imagens_plano_b(url, plataforma, pasta):
    urls = urls_imagem_instagram(url) if plataforma == "instagram" else urls_imagem_og(url)
    for i, u in enumerate(urls, 1):
        r = requests.get(u, headers=NAVEGADOR, timeout=30)
        if r.ok and r.headers.get("Content-Type", "").startswith("image/"):
            ext = ".png" if "png" in r.headers["Content-Type"] else ".webp" if "webp" in r.headers["Content-Type"] else ".jpg"
            with open(os.path.join(pasta, f"imagem_{i:02d}{ext}"), "wb") as f:
                f.write(r.content)


def arquivos_da_pasta(pasta, extensoes):
    achados = []
    for raiz, _, nomes in os.walk(pasta):
        for nome in sorted(nomes):
            if os.path.splitext(nome)[1].lower() in extensoes:
                achados.append(os.path.join(raiz, nome))
    return achados


def tem_anuncio():
    """True se templates/anuncio.html tiver algum código além de comentários."""
    caminho = os.path.join(app.root_path, "templates", "anuncio.html")
    if not os.path.exists(caminho):
        return False
    with open(caminho, encoding="utf-8") as f:
        texto = re.sub(r"<!--.*?-->", "", f.read(), flags=re.S)
    return bool(texto.strip())


@app.route("/")
def index():
    return render_template("index.html", tem_anuncio=tem_anuncio())


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
            # 1º: yt-dlp (mesmo caminho que já funciona para vídeos)
            try:
                salvar_imagens(urls_imagem_ytdlp(url), pasta)
            except Exception as e:  # noqa: BLE001
                app.logger.warning("yt-dlp (fotos) falhou em %s: %s", url, str(e)[:300])
            arquivos = arquivos_da_pasta(pasta, IMG_EXT)
            app.logger.info("yt-dlp achou %d imagem(ns) em %s", len(arquivos), url)
            # 2º: caminho rápido pela página pública do post
            if not arquivos:
                try:
                    baixar_imagens_plano_b(url, detectada, pasta)
                except requests.RequestException as e:
                    app.logger.warning("Caminho rápido falhou em %s: %s", url, e)
                arquivos = arquivos_da_pasta(pasta, IMG_EXT)
                app.logger.info("Caminho rápido achou %d imagem(ns) em %s", len(arquivos), url)
            # 3º: se ainda não achou nada, tenta o gallery-dl
            if not arquivos:
                try:
                    baixar_imagens(url, pasta)
                except (RuntimeError, subprocess.TimeoutExpired) as e:
                    app.logger.warning("gallery-dl falhou em %s: %s", url, str(e)[:300])
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
        return send_file(zip_mem, as_attachment=True, download_name=f"{detectada}_{'imagens' if tipo == 'imagem' else 'videos'}.zip",
                         mimetype="application/zip")

    except yt_dlp.utils.DownloadError as e:
        texto = str(e)
        app.logger.error("Falha yt-dlp em %s: %s", url, texto)
        if "login" in texto.lower() or "cookies" in texto.lower():
            return jsonify(erro="Esse post exige login. Veja no LEIA-ME como usar o cookies.txt."), 403
        return jsonify(erro="Não consegui baixar: " + texto.replace("ERROR: ", "")[:300]), 502
    except subprocess.TimeoutExpired:
        return jsonify(erro="O Instagram/Facebook não respondeu a tempo (provável bloqueio ao servidor). Tente de novo em alguns minutos."), 504
    except Exception as e:  # noqa: BLE001
        app.logger.error("Falha em %s: %s", url, e)
        return jsonify(erro="Não consegui baixar: " + str(e)[:300]), 502
    finally:
        shutil.rmtree(pasta, ignore_errors=True)


import logging
app.logger.setLevel(logging.INFO)

if __name__ == "__main__":
    print("Abra no navegador: http://localhost:5000")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
