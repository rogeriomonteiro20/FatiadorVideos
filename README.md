Fatiador IA de Vídeos

Ferramenta em Python que baixa um vídeo do YouTube, envia o áudio completo para o Google Gemini analisar e, a partir dessa análise, gera automaticamente:

Shorts verticais (9:16, até 59 segundos) para YouTube Shorts, TikTok e Reels, com troca de enquadramento entre 1 e 2 personagens;
Cortes horizontais longos (1080p, entre 10 e 14 minutos) que cobrem o vídeo inteiro;
Miniaturas (thumbnails) extraídas no momento de maior impacto de cada corte;
Legendas em PT-BR centralizadas na tela, aproveitando a legenda do próprio YouTube ou gerada pela IA.
Funcionalidades
Download do vídeo e da legenda automática em português via yt-dlp (até 1080p).
Análise do áudio completo pela IA, que sugere títulos virais, timestamps, motivo do corte e o melhor frame para a miniatura.
Enquadramento dinâmico: a IA analisa uma grade de frames (1 por segundo) e decide quando mostrar 1 personagem em tela cheia ou 2 personagens em tela dividida.
Legendas no formato .ass com estilo próprio, sincronizadas com o corte.
Rotação automática entre modelos Gemini disponíveis na sua chave, com novas tentativas em caso de erro 503/429.
FFmpeg embutido via imageio-ffmpeg, sem necessidade de instalação separada.
Requisitos
Python 3.9 ou superior
Uma chave de API do Google Gemini (obtida no Google AI Studio)
Conexão com a internet

Dependências:

bash
pip install google-genai pydantic pysubs2 yt-dlp imageio-ffmpeg
Configuração

A chave da API fica na constante GEMINI_API_KEY, no topo do arquivo. Não deixe a chave escrita diretamente no código, principalmente se o projeto for compartilhado ou enviado ao GitHub. A forma recomendada é ler a chave de uma variável de ambiente:

python
import os
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

Depois, defina a variável no seu sistema:

bash
# Linux / macOS
export GEMINI_API_KEY="sua_chave_aqui"

# Windows (PowerShell)
$env:GEMINI_API_KEY="sua_chave_aqui"

Se uma chave já foi publicada em algum lugar, revogue-a no Google AI Studio e gere uma nova.

Como usar

Execute o script:

bash
python fatiador.py

O programa fará três perguntas:

URL do YouTube. Cole o link do vídeo. Se deixar em branco, o programa usa o último .mp4 encontrado na pasta downloads/.
Formato do vídeo.
[1] Vertical 9:16 para Shorts (máximo de 59 s por corte).
[2] Horizontal 1080p com cortes longos de 10 a 14 minutos.
Legendas. Responda s para legendar em PT-BR no centro da tela, ou n para gerar sem legendas.
Estrutura de saída

Os arquivos são gravados em cortes_prontos/, numa pasta com o nome do vídeo e o formato:

cortes_prontos/
└── NomeDoVideo_9x16_shorts/          (ou NomeDoVideo_1080p_longos/)
    ├── Título do Corte.mp4
    ├── Título do Corte - Miniatura.jpg
    ├── 02 - Outro Título.mp4         (prefixo numérico se houver nome repetido)
    └── 02 - Outro Título - Miniatura.jpg

Os vídeos baixados ficam em downloads/, com o nome ID_DO_VIDEO.mp4.

Como funciona
Download (baixar_video): baixa o vídeo e a legenda PT/PT-BR do YouTube.
Análise (analisar_video_com_ia): extrai o áudio em mono, 16 kHz e 24 kbps para reduzir o envio, faz upload para o Gemini e pede uma lista de cortes em JSON validado pelo modelo ListaDeCortes. O programa testa os modelos disponíveis na ordem de preferência (Gemini 3 Flash, 2.0 Flash, 1.5 Flash, depois Pro e outros).
Direção de câmera (planejar_cameras_dinamicas): gera um mosaico de frames (1 por segundo) e envia à IA, que devolve as trocas entre 1 e 2 personagens.
Renderização (gerar_cortes): corta o trecho com FFmpeg, aplica enquadramento (tela cheia, tela dividida ou 16:9 completo), concatena as partes, adiciona legendas e extrai a miniatura.
Limitações conhecidas
Os cortes dependem da qualidade da análise do Gemini. Revise os títulos e timestamps antes de publicar.
Vídeos muito longos consomem mais tempo de upload e processamento.
A detecção de 1 ou 2 personagens é uma estimativa e pode errar em planos com pessoas fora de enquadramento.
As legendas geradas pela IA são feitas em blocos de 3 a 5 palavras e podem conter imprecisões de transcrição.
O script usa a pasta downloads/ como entrada padrão quando nenhuma URL é informada.
Algumas mensagens do programa estão em português europeu.
Aviso

Use a ferramenta apenas com conteúdo sobre o qual você tenha direitos ou autorização. Respeite os termos de uso do YouTube e as leis de direitos autorais da sua região.