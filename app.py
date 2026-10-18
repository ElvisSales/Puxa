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
from urllib.parse import parse_qs, quote, urlparse

import requests

from flask import Flask, jsonify, render_template, request, send_file
import yt_dlp

app = Flask(__name__, template_folder=".")

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


class EVideo(Exception):
    """O post é um vídeo."""


ROBO_FACEBOOK = {"User-Agent": "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)"}


def _link_do_post(texto):
    """Acha o endereço real do post dentro de um texto (URL de login, HTML ou mensagem de erro)."""
    from urllib.parse import unquote
    t = texto
    for _ in range(3):  # o endereço costuma vir codificado 1 ou 2 vezes
        t = unquote(t)
    t = t.replace("\\/", "/").replace("&amp;", "&")
    m = re.search(r"story_fbid=(\d+)&(?:amp;)?id=(\d+)", t) or re.search(r"[?&]id=(\d+)&(?:amp;)?story_fbid=(\d+)", t)
    if m:
        a, b = m.groups()
        post, dono = (a, b) if "story_fbid=" + a in t else (b, a)
        return f"https://www.facebook.com/{dono}/posts/{post}/"
    m = re.search(r"facebook\.com/groups/([\w.]+)/(?:permalink|posts)/(\d+)", t)
    if m:
        return f"https://www.facebook.com/groups/{m.group(1)}/permalink/{m.group(2)}/"
    m = re.search(r"facebook\.com/([\w.]+)/(posts|videos)/([\w]+)", t)
    if m and m.group(1) not in ("share", "login"):
        return f"https://www.facebook.com/{m.group(1)}/{m.group(2)}/{m.group(3)}/"
    m = re.search(r"facebook\.com/(?:photo(?:\.php)?/?\?fbid=|photo/\?fbid=)(\d+)", t)
    if m:
        return f"https://www.facebook.com/photo/?fbid={m.group(1)}"
    m = re.search(r"facebook\.com/(reel|watch/\?v=)/?(\d+)", t)
    if m:
        return f"https://www.facebook.com/reel/{m.group(2)}/" if m.group(1) == "reel" else f"https://www.facebook.com/watch/?v={m.group(2)}"
    return None


def resolver_facebook(url):
    """Links /share/... levam ao login, mas o endereço real do post vem junto.
    Devolve o link direto do post (ex.: facebook.com/<id>/posts/<post>)."""
    if not re.search(r"facebook\.com/share/|fb\.watch/|m\.facebook\.com|web\.facebook\.com", url):
        return url
    achado = None
    # 1º: segue os redirecionamentos com alguns "disfarces" diferentes
    for cab in (dict(NAVEGADOR, Accept="text/html,application/xhtml+xml"), ROBO_FACEBOOK, {"User-Agent": "Mozilla/5.0"}):
        try:
            r = requests.get(url, headers=cab, timeout=15, allow_redirects=True)
        except requests.RequestException as e:
            app.logger.warning("Resolver %s falhou: %s", url, e)
            continue
        candidatos = [h.headers.get("Location", "") for h in r.history] + [r.url, r.text[:300000]]
        for c in candidatos:
            achado = _link_do_post(c)
            if achado:
                break
        if achado:
            break
    # 2º: pergunta ao yt-dlp (ele sempre chega no link de login com o endereço real)
    if not achado:
        try:
            with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True, "socket_timeout": 15}) as ydl:
                info = ydl.extract_info(url, download=False, process=False)
            achado = _link_do_post(str(info.get("webpage_url") or info.get("url") or ""))
        except Exception as e:  # noqa: BLE001
            achado = _link_do_post(str(e))
    if achado:
        app.logger.info("Link do Facebook resolvido: %s -> %s", url, achado)
        return achado
    app.logger.warning("Não consegui resolver o link do Facebook: %s", url)
    return url

def _fotos_do_html_facebook(html):
    """Pega as fotos do post (ignora foto de perfil, emojis e ícones)."""
    urls = []
    for bruto in re.findall(r'<img[^>]+src="(https://scontent[^"]+)"', html):
        u = _limpar_url(bruto)
        if "t39.30808-1/" in u or "/p50x50/" in u or "/s40x40/" in u or "_s40x40" in u:
            continue  # foto de perfil
        if u not in urls:
            urls.append(u)
    return urls


def urls_imagem_facebook(url):
    """1º: a versão "incorporável" do post (a mesma que outros sites usam para mostrar posts públicos).
    2º: a prévia que o Facebook gera para o WhatsApp (og:image)."""
    # Versão incorporável
    embed = "https://www.facebook.com/plugins/post.php?href=" + quote(url, safe="") + "&show_text=false&width=500"
    try:
        html = requests.get(embed, headers=NAVEGADOR, timeout=20).text
        if re.search(r"<video|\"video_id\"|/videos/", html) and not _fotos_do_html_facebook(html):
            raise EVideo()
        urls = _fotos_do_html_facebook(html)
        app.logger.info("Embed do Facebook: %d foto(s) em %s (página com %d caracteres%s)", len(urls), url, len(html),
                        ", pede login" if "login" in html.lower()[:20000] else "")
        if urls:
            return urls[:20]
    except requests.RequestException as e:
        app.logger.warning("Embed do Facebook falhou em %s: %s", url, e)
    # Prévia (og:image)
    urls = []
    for cabecalho in (ROBO_FACEBOOK, NAVEGADOR):
        html = requests.get(url, headers=cabecalho, timeout=20, allow_redirects=True).text
        if re.search(r'property="og:video|property="og:type"[^>]+content="video|content="video[^"]*"[^>]+property="og:type"', html):
            app.logger.info("Prévia do Facebook indica vídeo em %s", url)
            raise EVideo()
        for padrao in (r'<meta[^>]+property="og:image(?::url)?"[^>]+content="([^"]+)"',
                       r'<meta[^>]+content="([^"]+)"[^>]+property="og:image(?::url)?"'):
            for achado in re.findall(padrao, html):
                u = _limpar_url(achado)
                if u.startswith("http") and u not in urls and "static.xx.fbcdn" not in u and "rsrc.php" not in u:
                    urls.append(u)
        if urls:
            break
    return urls

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


def arquivos_da_pasta(pasta, extensoes):
    achados = []
    for raiz, _, nomes in os.walk(pasta):
        for nome in sorted(nomes):
            if os.path.splitext(nome)[1].lower() in extensoes:
                achados.append(os.path.join(raiz, nome))
    return achados


def tem_anuncio():
    """True se anuncio.html tiver algum código além de comentários."""
    caminho = os.path.join(app.root_path, "anuncio.html")
    if not os.path.exists(caminho):
        return False
    with open(caminho, encoding="utf-8") as f:
        texto = re.sub(r"<!--.*?-->", "", f.read(), flags=re.S)
    return bool(texto.strip())


def buscar_videos(url, pasta):
    try:
        baixar_video(url, pasta)
    except yt_dlp.utils.DownloadError as e:
        texto = str(e)
        app.logger.warning("yt-dlp (vídeo) falhou em %s: %s", url, texto[:300])
        if "login" in texto.lower() and "no video" not in texto.lower() and "unsupported url" not in texto.lower():
            raise
    return arquivos_da_pasta(pasta, VID_EXT)


def buscar_imagens(url, plataforma, pasta):
    """Tenta vários caminhos, do mais rápido ao mais lento, até achar fotos."""
    if plataforma == "instagram":
        caminhos = [("yt-dlp", lambda: salvar_imagens(urls_imagem_ytdlp(url), pasta)),
                    ("embed", lambda: salvar_imagens(urls_imagem_instagram(url), pasta)),
                    ("gallery-dl", lambda: baixar_imagens(url, pasta))]
    else:
        caminhos = [("prévia do Facebook", lambda: salvar_imagens(urls_imagem_facebook(url), pasta)),
                    ("yt-dlp", lambda: salvar_imagens(urls_imagem_ytdlp(url), pasta)),
                    ("gallery-dl", lambda: baixar_imagens(url, pasta))]
    for nome, tentar in caminhos:
        try:
            tentar()
        except EVideo:
            return []
        except Exception as e:  # noqa: BLE001
            app.logger.warning("%s (fotos) falhou em %s: %s", nome, url, str(e)[:300])
        arquivos = arquivos_da_pasta(pasta, IMG_EXT)
        app.logger.info("%s achou %d imagem(ns) em %s", nome, len(arquivos), url)
        if arquivos:
            return arquivos
    return []


def buscar(tipo, url, plataforma, pasta):
    return buscar_videos(url, pasta) if tipo == "video" else buscar_imagens(url, plataforma, pasta)


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

    url_original = url
    if detectada == "facebook":
        url = resolver_facebook(url)

    pasta = tempfile.mkdtemp(prefix="baixador_")
    try:
        arquivos = buscar(tipo, url, detectada, pasta)
        if not arquivos and tipo == "video" and url != url_original:
            arquivos = buscar_videos(url_original, pasta)  # vídeos do Facebook já funcionavam com o link original
        if not arquivos:
            # Não achou o que foi pedido: tenta o outro tipo (ex.: escolheu Imagem mas o post é vídeo)
            outro = "video" if tipo == "imagem" else "imagem"
            app.logger.info("Nada de %s em %s; tentando %s", tipo, url, outro)
            arquivos = buscar(outro, url, detectada, pasta)
            if not arquivos and outro == "video" and url != url_original:
                arquivos = buscar_videos(url_original, pasta)
            if arquivos:
                tipo = outro

        if not arquivos:
            if detectada == "facebook":
                return jsonify(erro="Não consegui pegar esse post. O Facebook pode estar pedindo login para ele (post restrito ou de grupo)."), 404
            return jsonify(erro="Não encontrei foto nem vídeo nesse link. Confira se o post é público."), 404

        if len(arquivos) == 1:
            caminho = arquivos[0]
            with open(caminho, "rb") as f:
                conteudo = io.BytesIO(f.read())
            resp = send_file(conteudo, as_attachment=True, download_name=f"{detectada}_{os.path.basename(caminho)}")
            resp.headers["X-Tipo"] = tipo
            return resp

        # Vários arquivos (carrossel) → um .zip
        zip_mem = io.BytesIO()
        with zipfile.ZipFile(zip_mem, "w", zipfile.ZIP_DEFLATED) as z:
            for caminho in arquivos:
                z.write(caminho, os.path.basename(caminho))
        zip_mem.seek(0)
        resp = send_file(zip_mem, as_attachment=True, download_name=f"{detectada}_{'imagens' if tipo == 'imagem' else 'videos'}.zip",
                         mimetype="application/zip")
        resp.headers["X-Tipo"] = tipo
        return resp

    except yt_dlp.utils.DownloadError as e:
        texto = str(e)
        app.logger.error("Falha yt-dlp em %s: %s", url, texto)
        if "login" in texto.lower() or "cookies" in texto.lower():
            if detectada == "facebook":
                return jsonify(erro="O Facebook só mostra esse post para quem está logado (post restrito ou bloqueio ao servidor)."), 403
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
