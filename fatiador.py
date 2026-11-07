import glob
import json
import os
import re
import subprocess
import time
from google import genai
from google.genai import types
from google.genai.errors import APIError
import imageio_ffmpeg
from pydantic import BaseModel, Field
import pysubs2
import yt_dlp

# ==========================================
# CONFIGURAÇÃO DA CHAVE E DO FFMPEG EMBUTIDO
# ==========================================
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
CAMINHO_FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()


# ==========================================
# 1. ESTRUTURAS DE DADOS PARA O GEMINI
# ==========================================
class FraseLegenda(BaseModel):
  inicio_seg: float = Field(
      description="Segundos relativos ao início do corte (ex: 0.0, 2.0)"
  )
  fim_seg: float = Field(
      description="Segundos relativos ao início do corte (ex: 1.9, 4.0)"
  )
  texto: str = Field(description="Frase curta em PT-BR (2 a 5 palavras)")


class CorteSugerido(BaseModel):
  titulo: str = Field(
      description=(
          "Título altamente chamativo, curioso e viral em PT-BR, escrito"
          " normalmente com espaços e sem sublinhados (ex: O Gênio do QI"
          " Deixou Todo Mundo em Choque)"
      )
  )
  inicio: str = Field(
      description=(
          "Tempo exato de início no vídeo original no formato HH:MM:SS"
      )
  )
  fim: str = Field(
      description="Tempo exato de fim no vídeo original no formato HH:MM:SS"
  )
  segundo_melhor_frame_thumb: float = Field(
      description=(
          "Segundo relativo ao início do corte (ex: 12.5) onde ocorre o"
          " momento de maior expressão ou impacto visual para servir de"
          " miniatura (thumbnail)"
      )
  )
  motivo: str = Field(description="Por que este trecho dá um ótimo corte")
  legendas: list[FraseLegenda] = Field(
      default=[],
      description=(
          "Lista de legendas em PT-BR sincronizadas para este corte (apenas se"
          " solicitado)"
      ),
  )


class ListaDeCortes(BaseModel):
  cortes: list[CorteSugerido]


class SubCenaCamera(BaseModel):
  inicio_relativo: float = Field(
      description="Segundo relativo em que esta tomada começa (ex: 0.0)"
  )
  fim_relativo: float = Field(
      description="Segundo relativo em que esta tomada termina (ex: 5.0)"
  )
  quantidade_personagens_na_tela: int = Field(
      description=(
          "Digite 1 se houver apenas UM personagem falando ou em close-up."
          " Digite 2 SOMENTE se houver DOIS personagens de corpo/rosto visíveis"
          " simultaneamente lado a lado."
      )
  )
  dois_personagens: bool = Field(
      description=(
          "False (padrão) quando apenas 1 personagem aparece falando. True"
          " SOMENTE quando quantidade_personagens_na_tela for 2 e ambos"
          " estiverem afastados (um na esquerda e outro na direita)."
      )
  )
  cx_unico: float = Field(
      description=(
          "Posição horizontal (0.15 a 0.85) do centro do rosto do personagem"
          " que está falando sozinho (use 0.50 se estiver no centro da tela)."
      )
  )
  cx_topo: float = Field(
      description=(
          "Apenas se dois_personagens=True: posição horizontal (0.18 a 0.42) do"
          " rosto do personagem da esquerda."
      )
  )
  cy_topo: float = Field(
      description=(
          "Apenas se dois_personagens=True: posição vertical (0.20 a 0.65) do"
          " rosto do personagem da esquerda."
      )
  )
  cx_base: float = Field(
      description=(
          "Apenas se dois_personagens=True: posição horizontal (0.58 a 0.82) do"
          " rosto do personagem da direita."
      )
  )
  cy_base: float = Field(
      description=(
          "Apenas se dois_personagens=True: posição vertical (0.20 a 0.65) do"
          " rosto do personagem da direita."
      )
  )


class RoteiroCamera(BaseModel):
  cenas: list[SubCenaCamera]


# ==========================================
# AUXILIARES DE TEMPO, TEXTO E MODELOS
# ==========================================
def str_para_segundos(tempo_str):
  partes = [float(p) for p in tempo_str.strip().split(":")]
  if len(partes) == 3:
    return partes[0] * 3600 + partes[1] * 60 + partes[2]
  elif len(partes) == 2:
    return partes[0] * 60 + partes[1]
  return partes[0]


def segundos_para_str(seg):
  seg = max(0.0, float(seg))
  horas = int(seg // 3600)
  minutos = int((seg % 3600) // 60)
  segundos = seg % 60
  return f"{horas:02d}:{minutos:02d}:{segundos:05.2f}"


def obter_duracao_video(caminho_video):
  """Descobre a duração exata do vídeo em segundos usando o FFmpeg."""
  res = subprocess.run(
      [CAMINHO_FFMPEG, "-i", caminho_video],
      stdout=subprocess.PIPE,
      stderr=subprocess.PIPE,
      text=True,
      errors="ignore",
  )
  match = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", res.stderr)
  if match:
    h, m, s = (
        int(match.group(1)),
        int(match.group(2)),
        float(match.group(3)),
    )
    return h * 3600 + m * 60 + s
  return 3600.0


def limpar_nome_arquivo(titulo_bruto):
  """Mantém espaços e acentos normais, trocando "_" por espaço e removendo apenas caracteres proibidos pelo Windows."""
  texto = titulo_bruto.replace("_", " ").strip()
  texto = re.sub(r'[\\/*?:"<>|]', "", texto)
  texto = re.sub(r"\s+", " ", texto)
  return texto if texto else "Corte Viral"


def listar_modelos_validos(client):
  modelos_disponiveis = []
  for m in client.models.list():
    nome = m.name.replace("models/", "")
    acoes = getattr(m, "supported_actions", []) or []
    if "gemini" in nome and (not acoes or "generateContent" in acoes):
      if not any(
          x in nome for x in ["embedding", "imagen", "veo", "2.5-flash", "tts"]
      ):
        modelos_disponiveis.append(nome)

  preferencias = [
      "gemini-3-flash",
      "gemini-2.0-flash",
      "gemini-1.5-flash",
      "gemini-3-pro",
      "flash",
      "pro",
  ]
  ordenados = []
  for pref in preferencias:
    for mod in modelos_disponiveis:
      if pref in mod and mod not in ordenados:
        ordenados.append(mod)

  for mod in modelos_disponiveis:
    if mod not in ordenados:
      ordenados.append(mod)

  return ordenados if ordenados else ["gemini-2.0-flash"]


# ==========================================
# 2. DOWNLOAD DO VÍDEO (COM FFMPEG INTEGRADO)
# ==========================================
def baixar_video(url, pasta_saida="downloads"):
  os.makedirs(pasta_saida, exist_ok=True)
  caminho_template = os.path.join(pasta_saida, "%(id)s.%(ext)s")

  opcoes_video = {
      "format": (
          "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"
      ),
      "outtmpl": caminho_template,
      "merge_output_format": "mp4",
      "ffmpeg_location": CAMINHO_FFMPEG,
      "quiet": False,
  }

  print("\n[1/3] A transferir vídeo do YouTube...")
  with yt_dlp.YoutubeDL(opcoes_video) as ydl:
    info = ydl.extract_info(url, download=True)
    video_id = info.get("id")
    caminho_mp4 = os.path.join(pasta_saida, f"{video_id}.mp4")

  opcoes_sub = {
      "skip_download": True,
      "writesubtitles": True,
      "writeautomaticsub": True,
      "sublangs": ["pt", "pt-BR"],
      "subtitlesformat": "vtt",
      "outtmpl": caminho_template,
      "ignoreerrors": True,
      "quiet": True,
  }
  try:
    with yt_dlp.YoutubeDL(opcoes_sub) as ydl:
      ydl.download([url])
  except Exception:
    pass

  return caminho_mp4


def localizar_legenda_local(caminho_video):
  base, _ = os.path.splitext(caminho_video)
  possiveis = glob.glob(f"{base}*.pt*.vtt") + glob.glob(f"{base}*.pt*.srt")
  return possiveis[0] if possiveis else None


# ==========================================
# 3. IA ANALISA O VÍDEO TODO E GERA MÚLTIPLOS CORTES
# ==========================================
def analisar_video_com_ia(
    caminho_video, vertical=True, gerar_legenda_ia=False
):
  duracao_video_s = obter_duracao_video(caminho_video)
  duracao_min = duracao_video_s / 60.0
  duracao_hms = segundos_para_str(duracao_video_s)

  print(
      f"\n[2/3] A preparar áudio completo ({duracao_min:.1f} min) para a IA"
      " analisar do início ao fim..."
  )
  caminho_audio = "temp_audio_ia.mp3"

  # Usa bitrate de 24k para que até podcasts de 3 horas fiquem leves e rápidos de enviar
  subprocess.run(
      [
          CAMINHO_FFMPEG,
          "-y",
          "-i",
          caminho_video,
          "-vn",
          "-ac",
          "1",
          "-ar",
          "16000",
          "-b:a",
          "24k",
          caminho_audio,
      ],
      stdout=subprocess.DEVNULL,
      stderr=subprocess.DEVNULL,
  )

  client = genai.Client(api_key=GEMINI_API_KEY.strip())
  lista_modelos = listar_modelos_validos(client)

  if vertical:
    regra_tempo = (
        "- DURAÇÃO ESTRITA DE SHORTS: Cada corte deve ter entre 35 segundos e"
        " NO MÁXIMO 59 SEGUNDOS (NUNCA passe de 59 segundos!).\n    -"
        " QUANTIDADE: Analise o vídeo inteiro do início ao fim e extraia"
        " múltiplos Shorts (todos os momentos realmente virais que encontrar)."
    )
  else:
    # Calcula quantos cortes de 10-14 min cabem proporcionalmente no vídeo longo
    min_cortes_longos = max(2, int(duracao_min // 18))
    max_cortes_longos = max(4, int(duracao_min // 11))
    if duracao_min < 25:
      min_cortes_longos = max(1, int(duracao_min // 10))
      max_cortes_longos = max(2, int(duracao_min // 8))

    regra_tempo = (
        "- DURAÇÃO DOS CORTES LONGOS: Cada corte deve ser um bloco completo de"
        " assunto/história tendo entre 10 minutos e 14 minutos de duração.\n   "
        " - PROIBIDO GERAR APENAS 1 CORTE: Este vídeo tem"
        f" {duracao_min:.1f} minutos (termina em {duracao_hms}). Você DEVE"
        " analisar o vídeo INTEIRO (início, meio e fim) e gerar ENTRE"
        f" {min_cortes_longos} E {max_cortes_longos} CORTES LONGOS diferentes,"
        " cobrindo todas as pautas e debates ao longo de toda a gravação!"
    )

  regra_legenda = (
      "- Preenche também o campo 'legendas' transcrevendo ou traduzindo a fala"
      " para PT-BR dividida em blocos curtos (3 a 5 palavras por vez) com"
      " inicio_seg e fim_seg relativos ao início do corte (começando em 0.0)."
      if (gerar_legenda_ia and vertical)
      else "- Deixa a lista 'legendas' vazia []."
  )

  prompt = f"""
    Ouve esta gravação completa (duração total: {duracao_hms}) do primeiro ao último minuto e atua como um estrategista e editor profissional de canais de cortes no YouTube e TikTok.
    Varre o áudio inteiro cronologicamente sem parar no primeiro trecho!

    Regras obrigatórias:
    {regra_tempo}
    - O corte precisa de começar num gancho forte e terminar na conclusão do raciocínio.
    - CAPRICHA MUITO NOS TÍTULOS (`titulo`): Cria títulos magnéticos, instigantes e virais em português (ex: "O Gênio do QI Explicou Tudo", "A Resposta Que Deixou Ele Sem Palavras").
    - NUNCA uses sublinhados (_) nos títulos! Escreve normalmente com espaços entre as palavras.
    - Indica em `segundo_melhor_frame_thumb` qual o segundo relativo dentro do corte (ex: 15.0) onde acontece o clímax ou melhor momento para extrairmos uma miniatura (thumbnail).
    - Fornece os timestamps exatos de início e fim no formato HH:MM:SS (ex: 00:15:30, 01:22:10).
    {regra_legenda}
    """

  print("      -> A enviar áudio para a nuvem do Gemini...")
  arquivo_upload = client.files.upload(file=caminho_audio)

  while arquivo_upload.state.name == "PROCESSING":
    time.sleep(2)
    arquivo_upload = client.files.get(name=arquivo_upload.name)

  ultimo_erro = None
  for modelo_ativo in lista_modelos:
    for tentativa in range(1, 4):
      try:
        print(
            f"      -> A IA ({modelo_ativo} | tentativa {tentativa}/3) está a"
            " analisar o vídeo todo e a escolher os cortes..."
        )
        response = client.models.generate_content(
            model=modelo_ativo,
            contents=[arquivo_upload, prompt],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=ListaDeCortes,
                temperature=0.4,
                max_output_tokens=8192,
            ),
        )

        client.files.delete(name=arquivo_upload.name)
        if os.path.exists(caminho_audio):
          os.remove(caminho_audio)

        dados = json.loads(response.text)
        print(
            f"      -> A IA selecionou {len(dados['cortes'])} cortes ao longo"
            " do vídeo!"
        )
        return dados["cortes"], modelo_ativo

      except APIError as e:
        ultimo_erro = e
        erro_str = str(e)
        if "503" in erro_str or "UNAVAILABLE" in erro_str or "429" in erro_str:
          espera = tentativa * 5
          print(
              f"         [Aviso] Servidor ocupado (503/429). A aguardar"
              f" {espera}s..."
          )
          time.sleep(espera)
        else:
          break

  if os.path.exists(caminho_audio):
    os.remove(caminho_audio)
  print("\n[ERRO DETALHADO DA API]:", ultimo_erro)
  raise SystemExit(1)


# ==========================================
# 4. DIRETOR DE CÂMARA DINÂMICO (FOCO EM 1 SÓ QUANDO FALAR SOZINHO)
# ==========================================
def planejar_cameras_dinamicas(caminho_video, inicio_s, fim_s, modelo_ativo):
  duracao = max(2.0, fim_s - inicio_s)
  painel_temp = "temp_painel_cenas.jpg"

  filtro_mosaico = "fps=1,scale=320:180,tile=6x10"

  subprocess.run(
      [
          CAMINHO_FFMPEG,
          "-y",
          "-ss",
          f"{inicio_s:.2f}",
          "-t",
          f"{duracao:.2f}",
          "-i",
          caminho_video,
          "-vf",
          filtro_mosaico,
          "-frames:v",
          "1",
          "-q:v",
          "3",
          painel_temp,
      ],
      stdout=subprocess.DEVNULL,
      stderr=subprocess.DEVNULL,
  )

  padrao = [{
      "inicio_relativo": 0.0,
      "fim_relativo": duracao,
      "quantidade_personagens_na_tela": 1,
      "dois_personagens": False,
      "cx_unico": 0.50,
      "cx_topo": 0.25,
      "cy_topo": 0.40,
      "cx_base": 0.75,
      "cy_base": 0.40,
  }]

  if not os.path.exists(painel_temp):
    return padrao

  try:
    client = genai.Client(api_key=GEMINI_API_KEY.strip())
    with open(painel_temp, "rb") as f:
      img_bytes = f.read()

    prompt_cam = f"""
        Esta imagem é uma grade (6 colunas por 10 linhas, lida da esquerda para a direita, de cima para baixo) onde CADA MINIATURA representa exatamente 1 SEGUNDO do vídeo (de 0s até {duracao:.0f}s).
        Ex: A 1ª linha contém os segundos 0, 1, 2, 3, 4, 5. A 2ª linha contém os segundos 6, 7, 8, 9, 10, 11, e assim por diante.

        A sua tarefa mais importante é evitar dividir a tela quando houver apenas 1 personagem! Siga estas regras rigorosas:

        1. QUANDO APARECER SÓ 1 PERSONAGEM NA MINIATURA (Close-up, plano médio de 1 pessoa, ou apenas 1 rosto em destaque):
           - OBRIGATÓRIO: `quantidade_personagens_na_tela = 1` e `dois_personagens = false`.
           - Informe `cx_unico` (entre 0.16 e 0.84) apontando exatamente para o centro do rosto desse personagem (0.50 se estiver no meio da tela).
           - Em caso de dúvida se há 1 ou 2 personagens, escolha SEMPRE 1 personagem (`dois_personagens = false`)!

        2. SOMENTE QUANDO APARECEREM 2 PERSONAGENS INTEIROS LADO A LADO CONVERSANDO (um claramente na metade esquerda e outro claramente na metade direita):
           - Defina `quantidade_personagens_na_tela = 2` e `dois_personagens = true`.
           - Informe `cx_topo` (entre 0.18 e 0.42) e `cy_topo` (entre 0.25 e 0.60) para o rosto do personagem da esquerda.
           - Informe `cx_base` (entre 0.58 e 0.82) e `cy_base` (entre 0.25 e 0.60) para o rosto do personagem da direita.

        Divida o corte de 0.0 até {duracao:.1f}s em sub-cenas precisas sempre que a câmara alternar entre 1 personagem e 2 personagens.
        """

    resp = client.models.generate_content(
        model=modelo_ativo,
        contents=[
            types.Part.from_bytes(data=img_bytes, mime_type="image/jpeg"),
            prompt_cam,
        ],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=RoteiroCamera,
            temperature=0.0,
        ),
    )
    os.remove(painel_temp)
    cenas = json.loads(resp.text).get("cenas", [])
    return cenas if cenas else padrao
  except Exception:
    if os.path.exists(painel_temp):
      os.remove(painel_temp)
    return padrao


# ==========================================
# 5. CRIAÇÃO DE LEGENDA NO CENTRO DA TELA (.ASS)
# ==========================================
def criar_legenda_corte(
    corte, inicio_s, fim_s, caminho_ass, vertical, legenda_yt_path=None
):
  subs = pysubs2.SSAFile()
  subs.info["PlayResX"] = "1080" if vertical else "1920"
  subs.info["PlayResY"] = "1920" if vertical else "1080"

  estilo = pysubs2.SSAStyle(
      fontname="Arial",
      fontsize=74 if vertical else 50,
      primarycolor=pysubs2.Color(255, 255, 255, 0),
      outlinecolor=pysubs2.Color(0, 0, 0, 0),
      backcolor=pysubs2.Color(0, 0, 0, 140),
      bold=True,
      outline=6.0,
      shadow=3.0,
      alignment=(
          pysubs2.Alignment.MIDDLE_CENTER
          if vertical
          else pysubs2.Alignment.BOTTOM_CENTER
      ),
      marginv=0 if vertical else 70,
  )
  subs.styles["CortesCentro"] = estilo

  if legenda_yt_path and os.path.exists(legenda_yt_path):
    sub_original = pysubs2.load(legenda_yt_path)
    for ev in sub_original.events:
      ev_ini = ev.start / 1000.0
      ev_fim = ev.end / 1000.0
      if ev_fim > inicio_s and ev_ini < fim_s:
        texto_limpo = ev.plaintext.replace("\n", " ").strip().lower()
        if texto_limpo:
          palavras = texto_limpo.split()
          bloco_tam = 4
          duracao_total = max(0.3, ev_fim - ev_ini)
          qtd_blocos = max(1, (len(palavras) + bloco_tam - 1) // bloco_tam)
          tempo_por_bloco = duracao_total / qtd_blocos

          for b_idx in range(qtd_blocos):
            trecho = " ".join(
                palavras[b_idx * bloco_tam : (b_idx + 1) * bloco_tam]
            )
            t_i = max(0.0, (ev_ini - inicio_s) + b_idx * tempo_por_bloco)
            t_f = max(t_i + 0.2, t_i + tempo_por_bloco)
            novo_ev = pysubs2.SSAEvent(
                start=pysubs2.make_time(s=t_i),
                end=pysubs2.make_time(s=t_f),
                text=trecho,
                style="CortesCentro",
            )
            subs.events.append(novo_ev)

  elif corte.get("legendas"):
    for item in corte["legendas"]:
      if float(item["inicio_seg"]) >= (fim_s - inicio_s):
        continue
      evento = pysubs2.SSAEvent(
          start=pysubs2.make_time(s=max(0.0, float(item["inicio_seg"]))),
          end=pysubs2.make_time(
              s=min(fim_s - inicio_s, max(0.2, float(item["fim_seg"])))
          ),
          text=item["texto"].lower(),
          style="CortesCentro",
      )
      subs.events.append(evento)

  if len(subs.events) > 0:
    subs.save(caminho_ass)
    return True
  return False


# ==========================================
# 6. EXTRAÇÃO DE MINIATURA (THUMBNAIL)
# ==========================================
def extrair_miniatura_corte(
    caminho_video_fonte, segundo_relativo, duracao_total, caminho_jpg
):
  """Extrai um frame em alta qualidade para servir de capa/miniatura do corte."""
  t_thumb = max(
      1.0,
      min(duracao_total - 1.0, float(segundo_relativo or duracao_total / 2)),
  )
  cmd_thumb = [
      CAMINHO_FFMPEG,
      "-y",
      "-ss",
      f"{t_thumb:.2f}",
      "-i",
      caminho_video_fonte,
      "-frames:v",
      "1",
      "-q:v",
      "2",
      caminho_jpg,
  ]
  subprocess.run(
      cmd_thumb, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
  )


# ==========================================
# 7. RENDERIZAÇÃO COM TRAVA DE 59s EM SHORTS
# ==========================================
def gerar_cortes(
    caminho_video,
    cortes_ia,
    modelo_ativo,
    pasta_base="cortes_prontos",
    vertical=False,
    legendar=False,
    legenda_yt=None,
):
  nome_video = os.path.splitext(os.path.basename(caminho_video))[0]
  sufixo_formato = "9x16_shorts" if vertical else "1080p_longos"
  pasta_video = os.path.join(pasta_base, f"{nome_video}_{sufixo_formato}")
  os.makedirs(pasta_video, exist_ok=True)

  print(
      f"\n[3/3] A gerar os {len(cortes_ia)} cortes e miniaturas na pasta"
      f" '{pasta_video}'..."
  )

  for idx, corte in enumerate(cortes_ia, 1):
    inicio = corte["inicio"]
    fim = corte["fim"]
    inicio_s = str_para_segundos(inicio)
    fim_s = str_para_segundos(fim)

    # TRAVA RÍGIDA DE 59 SEGUNDOS PARA SHORTS (9:16)
    if vertical and (fim_s - inicio_s) > 59.0:
      fim_s = inicio_s + 59.0
      fim = segundos_para_str(fim_s)

    duracao_total = max(1.0, fim_s - inicio_s)

    titulo_limpo = limpar_nome_arquivo(corte["titulo"])

    arquivo_mp4 = os.path.join(pasta_video, f"{titulo_limpo}.mp4")
    arquivo_thumb = os.path.join(pasta_video, f"{titulo_limpo} - Miniatura.jpg")
    if os.path.exists(arquivo_mp4):
      arquivo_mp4 = os.path.join(pasta_video, f"{idx:02d} - {titulo_limpo}.mp4")
      arquivo_thumb = os.path.join(
          pasta_video, f"{idx:02d} - {titulo_limpo} - Miniatura.jpg"
      )

    arquivo_ass = os.path.join(pasta_video, f"temp_{idx}.ass")
    segundo_thumb = corte.get("segundo_melhor_frame_thumb", duracao_total / 2)

    print(
        f"\n  -> Corte {idx}/{len(cortes_ia)}: {titulo_limpo} ({inicio} até"
        f" {fim} | {duracao_total:.0f}s)"
    )
    print(f"     Motivo: {corte['motivo']}")

    tem_legenda = False
    if legendar:
      tem_legenda = criar_legenda_corte(
          corte, inicio_s, fim_s, arquivo_ass, vertical, legenda_yt
      )

    caminho_ass_ffmpeg = arquivo_ass.replace("\\", "/").replace(":", "\\:")

    if vertical:
      print(
          "     -> A analisar trocas de câmara segundo a segundo (1 vs 2"
          " personagens)..."
      )
      cenas = planejar_cameras_dinamicas(
          caminho_video, inicio_s, fim_s, modelo_ativo
      )

      arquivos_partes = []
      lista_concat_txt = os.path.join(pasta_video, f"concat_{idx}.txt")

      for c_idx, cena in enumerate(cenas):
        t_ini_rel = max(0.0, float(cena.get("inicio_relativo", 0.0)))
        t_fim_rel = min(
            duracao_total, float(cena.get("fim_relativo", duracao_total))
        )
        if c_idx == len(cenas) - 1:
          t_fim_rel = duracao_total
        if t_fim_rel - t_ini_rel < 0.4:
          continue

        t_abs_ini = inicio_s + t_ini_rel
        dur_sub = t_fim_rel - t_ini_rel
        parte_mp4 = os.path.join(pasta_video, f"temp_p_{idx}_{c_idx}.mp4")

        qtd_p = int(cena.get("quantidade_personagens_na_tela", 1))
        dois_p = bool(cena.get("dois_personagens", False)) and (qtd_p == 2)
        cx1 = float(cena.get("cx_topo", 0.28))
        cx2 = float(cena.get("cx_base", 0.72))

        if dois_p and (abs(cx2 - cx1) < 0.30 or cx1 > 0.45 or cx2 < 0.55):
          dois_p = False

        if dois_p:
          print(
              f"        [{t_ini_rel:.1f}s - {t_fim_rel:.1f}s] 2 Personagens ->"
              " Tela Dividida"
          )
          cx1 = max(0.18, min(0.42, cx1))
          cy1 = max(0.25, min(0.68, float(cena.get("cy_topo", 0.42))))
          cx2 = max(0.58, min(0.82, cx2))
          cy2 = max(0.25, min(0.68, float(cena.get("cy_base", 0.42))))

          fx1 = max(0.0, min(0.58, cx1 - 0.21))
          fy1 = max(0.0, min(0.34, cy1 - 0.33))
          fx2 = max(0.0, min(0.58, cx2 - 0.21))
          fy2 = max(0.0, min(0.34, cy2 - 0.33))

          crop_top = f"crop=iw*0.42:ih*0.66:iw*{fx1:.3f}:ih*{fy1:.3f},scale=1080:960,setsar=1"
          crop_bot = f"crop=iw*0.42:ih*0.66:iw*{fx2:.3f}:ih*{fy2:.3f},scale=1080:960,setsar=1"
          filtro_sub = f"[0:v]split=2[v1][v2];[v1]{crop_top}[top];[v2]{crop_bot}[bottom];[top][bottom]vstack=inputs=2[vout]"

          cmd_sub = [
              CAMINHO_FFMPEG,
              "-y",
              "-ss",
              f"{t_abs_ini:.2f}",
              "-t",
              f"{dur_sub:.2f}",
              "-i",
              caminho_video,
              "-filter_complex",
              filtro_sub,
              "-map",
              "[vout]",
              "-map",
              "0:a?",
              "-c:v",
              "libx264",
              "-preset",
              "ultrafast",
              "-crf",
              "22",
              "-c:a",
              "aac",
              "-ar",
              "44100",
              "-ac",
              "2",
              parte_mp4,
          ]
        else:
          cx_u = max(0.16, min(0.84, float(cena.get("cx_unico", 0.50))))
          print(
              f"        [{t_ini_rel:.1f}s - {t_fim_rel:.1f}s] 1 Personagem ->"
              f" Tela Cheia Centralizada (X={cx_u:.2f})"
          )
          fx_u = max(0.0, min(0.683, cx_u - 0.158))
          filtro_vf = (
              f"crop=ih*9/16:ih:iw*{fx_u:.3f}:0,scale=1080:1920,setsar=1"
          )

          cmd_sub = [
              CAMINHO_FFMPEG,
              "-y",
              "-ss",
              f"{t_abs_ini:.2f}",
              "-t",
              f"{dur_sub:.2f}",
              "-i",
              caminho_video,
              "-vf",
              filtro_vf,
              "-c:v",
              "libx264",
              "-preset",
              "ultrafast",
              "-crf",
              "22",
              "-c:a",
              "aac",
              "-ar",
              "44100",
              "-ac",
              "2",
              parte_mp4,
          ]

        subprocess.run(
            cmd_sub, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        if os.path.exists(parte_mp4) and os.path.getsize(parte_mp4) > 0:
          arquivos_partes.append(parte_mp4)

      with open(lista_concat_txt, "w", encoding="utf-8") as f_txt:
        for p in arquivos_partes:
          caminho_abs = os.path.abspath(p).replace("\\", "/")
          f_txt.write(f"file '{caminho_abs}'\n")

      video_limpo_temp = os.path.join(pasta_video, f"temp_limpo_{idx}.mp4")
      subprocess.run(
          [
              CAMINHO_FFMPEG,
              "-y",
              "-f",
              "concat",
              "-safe",
              "0",
              "-i",
              lista_concat_txt,
              "-c",
              "copy",
              video_limpo_temp,
          ],
          stdout=subprocess.DEVNULL,
          stderr=subprocess.DEVNULL,
      )

      if os.path.exists(video_limpo_temp):
        extrair_miniatura_corte(
            video_limpo_temp, segundo_thumb, duracao_total, arquivo_thumb
        )

      if tem_legenda and os.path.exists(video_limpo_temp):
        cmd_final = [
            CAMINHO_FFMPEG,
            "-y",
            "-i",
            video_limpo_temp,
            "-vf",
            f"ass='{caminho_ass_ffmpeg}'",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-crf",
            "22",
            "-c:a",
            "copy",
            arquivo_mp4,
        ]
        resultado = subprocess.run(
            cmd_final, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        os.remove(video_limpo_temp)
      elif os.path.exists(video_limpo_temp):
        os.replace(video_limpo_temp, arquivo_mp4)
        resultado = None

      for p in arquivos_partes:
        if os.path.exists(p):
          os.remove(p)
      if os.path.exists(lista_concat_txt):
        os.remove(lista_concat_txt)

    else:
      t_abs_thumb = inicio_s + max(
          1.0,
          min(duracao_total - 1.0, float(segundo_thumb or duracao_total / 2)),
      )
      subprocess.run(
          [
              CAMINHO_FFMPEG,
              "-y",
              "-ss",
              f"{t_abs_thumb:.2f}",
              "-i",
              caminho_video,
              "-vf",
              "scale=1920:1080",
              "-frames:v",
              "1",
              "-q:v",
              "2",
              arquivo_thumb,
          ],
          stdout=subprocess.DEVNULL,
          stderr=subprocess.DEVNULL,
      )

      filtro_vf = "scale=1920:1080"
      if tem_legenda:
        filtro_vf += f",ass='{caminho_ass_ffmpeg}'"

      comando = [
          CAMINHO_FFMPEG,
          "-y",
          "-ss",
          f"{inicio_s:.2f}",
          "-t",
          f"{duracao_total:.2f}",
          "-i",
          caminho_video,
          "-vf",
          filtro_vf,
          "-c:v",
          "libx264",
          "-preset",
          "ultrafast",
          "-crf",
          "23",
          "-c:a",
          "aac",
          arquivo_mp4,
      ]
      resultado = subprocess.run(
          comando, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
      )

    if os.path.exists(arquivo_ass):
      os.remove(arquivo_ass)

    if os.path.exists(arquivo_mp4) and os.path.getsize(arquivo_mp4) > 0:
      print(f"     [OK] Vídeo salvo em: {arquivo_mp4}")
      if os.path.exists(arquivo_thumb):
        print(f"     [OK] Miniatura salva em: {arquivo_thumb}")
    else:
      print("     [ERRO FFMPEG] O ficheiro não foi gerado. Detalhe:")
      if resultado and resultado.stderr:
        print("\n".join(resultado.stderr.splitlines()[-5:]))

  return pasta_video


# ==========================================
# MENU INTERATIVO PRINCIPAL
# ==========================================
if __name__ == "__main__":
  print("=== GERADOR DE CORTES IA (SHORTS <= 59s & MÚLTIPLOS CORTES LONGOS) ===")
  url = input(
      "1. Cola a URL do YouTube (ou prime ENTER para usar vídeo já transferido):"
      " "
  ).strip()

  print("\n2. Qual o formato do vídeo?")
  print(
      "   [1] 9:16 Vertical Shorts (Máximo 59s + Alternância 1 ou 2"
      " Personagens)"
  )
  print(
      "   [2] 1080p Horizontal (Múltiplos Cortes Longos de 10m a 14m pelo"
      " vídeo todo)"
  )
  op_formato = input("   Escolhe (1 ou 2): ").strip()
  vertical = op_formato == "1"

  op_legenda = (
      input(
          "\n3. Desejas legendar os vídeos em PT-BR no centro da tela? (s/n): "
      )
      .strip()
      .lower()
  )
  legendar = op_legenda == "s"

  if url:
    video_local = baixar_video(url)
  else:
    arquivos = [
        os.path.join("downloads", f)
        for f in os.listdir("downloads")
        if f.endswith(".mp4")
    ]
    if not arquivos:
      print("Nenhum vídeo encontrado na pasta 'downloads'.")
      raise SystemExit(1)
    video_local = arquivos[-1]
    print(f"A usar vídeo local: {video_local}")

  legenda_yt = localizar_legenda_local(video_local) if legendar else None
  if legendar and not legenda_yt:
    print(
        "   [!] Sem legenda PT-BR do YouTube. A pedir à IA para gerar/traduzir"
        " a legenda..."
    )
    gerar_legenda_ia = True
  else:
    gerar_legenda_ia = False

  cortes_escolhidos, modelo_usado = analisar_video_com_ia(
      video_local, vertical=vertical, gerar_legenda_ia=gerar_legenda_ia
  )
  pasta_final = gerar_cortes(
      video_local,
      cortes_escolhidos,
      modelo_usado,
      vertical=vertical,
      legendar=legendar,
      legenda_yt=legenda_yt,
  )

  print(
      "\n[✔] Processo finalizado! Os teus vídeos e miniaturas estão na pasta:"
      f" '{pasta_final}'"
  )