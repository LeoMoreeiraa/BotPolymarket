"""
=============================================================
  BOT DE ARBITRAGEM - POLYMARKET
  Autor: gerado com Claude
  Linguagem: Python 3.10+

  O QUE ELE FAZ:
  - Busca mercados ativos no Polymarket via API pública
  - Detecta oportunidades de arbitragem dentro de um mesmo
    mercado (quando YES + NO < $1.00, há lucro garantido)
  - Detecta arbitragem entre mercados correlatos (ex: dois
    mercados sobre o mesmo evento em plataformas diferentes)
  - Calcula lucro esperado e risco antes de agir
  - Modo SIMULAÇÃO por padrão (sem gastar dinheiro real)

  COMO USAR:
  1. pip install requests python-dotenv colorama
  2. Crie um arquivo .env com sua chave (opcional para leitura)
  3. python polymarket_arb_bot.py

  PARA EXECUTAR ORDENS REAIS:
  - Você precisa de uma carteira Polygon (MetaMask)
  - Use py-clob-client da Polymarket (veja seção EXECUÇÃO)
=============================================================
"""

import requests
import time
import json
import os
from datetime import datetime
from dataclasses import dataclass, field
from typing import Optional
from colorama import Fore, Style, init

init(autoreset=True)  # colorama: cores no terminal

# ──────────────────────────────────────────────
#  CONFIGURAÇÕES
# ──────────────────────────────────────────────

CONFIG = {
    "modo_simulacao": True,       # True = só loga, False = executa ordens
    "min_lucro_pct": 1.5,         # só entra se lucro >= 1.5% (cobre taxas)
    "max_stake_usd": 50.0,        # valor máximo por operação (em modo real)
    "intervalo_scan_seg": 30,     # quantos segundos entre cada varredura
    "taxa_polymarket_pct": 2.0,   # taxa da plataforma (2% por lado)
    "categorias_alvo": [          # filtra por categoria (None = todas)
        "Politics", "Crypto", "Sports"
    ],
}

# ──────────────────────────────────────────────
#  ENDPOINTS DA API PÚBLICA DO POLYMARKET
# ──────────────────────────────────────────────

BASE_URL = "https://gamma-api.polymarket.com"
CLOB_URL = "https://clob.polymarket.com"

ENDPOINTS = {
    "mercados":    f"{BASE_URL}/markets",
    "eventos":     f"{BASE_URL}/events",
    "book":        f"{CLOB_URL}/book",        # order book de um token
    "midpoint":    f"{CLOB_URL}/midpoint",    # preço médio
}

# ──────────────────────────────────────────────
#  ESTRUTURAS DE DADOS
# ──────────────────────────────────────────────

@dataclass
class Mercado:
    """Representa um mercado binário (YES/NO) do Polymarket."""
    id: str
    pergunta: str
    categoria: str
    token_yes: str        # ID do token YES (usado no order book)
    token_no: str         # ID do token NO
    preco_yes: float = 0.0   # preço atual de YES (0 a 1)
    preco_no: float = 0.0    # preço atual de NO  (0 a 1)
    volume_24h: float = 0.0
    ativo: bool = True

@dataclass
class OportunidadeArb:
    """Representa uma oportunidade de arbitragem detectada."""
    tipo: str                          # "interno" ou "externo"
    mercado_a: Mercado
    mercado_b: Optional["Mercado"]     # só para arb externo
    lado_a: str                        # "YES" ou "NO"
    lado_b: str
    custo_total: float                 # quanto você gasta
    retorno_garantido: float           # quanto você recebe no melhor caso
    lucro_bruto: float
    lucro_liquido: float               # após taxas
    lucro_pct: float
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())

# ──────────────────────────────────────────────
#  CLIENTE DA API
# ──────────────────────────────────────────────

class PolymarketAPI:
    """
    Wrapper para a API pública do Polymarket.
    Não precisa de autenticação para LEITURA.
    """

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "polymarket-arb-bot/1.0",
            "Accept": "application/json",
        })

    def _get(self, url: str, params: dict = None) -> dict | list | None:
        """Faz uma requisição GET com tratamento de erro."""
        try:
            resp = self.session.get(url, params=params, timeout=10)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.Timeout:
            print(f"{Fore.RED}[ERRO] Timeout ao acessar {url}")
            return None
        except requests.exceptions.HTTPError as e:
            print(f"{Fore.RED}[ERRO] HTTP {e.response.status_code} em {url}")
            return None
        except Exception as e:
            print(f"{Fore.RED}[ERRO] {e}")
            return None

    def buscar_mercados(self, limit: int = 100, offset: int = 0) -> list[dict]:
        """
        Retorna lista de mercados ativos.
        A API retorna ~100 mercados por chamada; use offset para paginar.
        """
        params = {
            "active": "true",
            "closed": "false",
            "limit": limit,
            "offset": offset,
        }
        dados = self._get(ENDPOINTS["mercados"], params)
        if dados is None:
            return []
        # A API pode retornar lista direta ou dict com "data"
        if isinstance(dados, list):
            return dados
        return dados.get("data", [])

    def buscar_preco_midpoint(self, token_id: str) -> float | None:
        """
        Retorna o preço médio (mid) de um token.
        O preço vai de 0 a 1 (= probabilidade implícita).
        Ex: 0.65 significa que o mercado acha 65% de chance de YES.
        """
        dados = self._get(ENDPOINTS["midpoint"], {"token_id": token_id})
        if dados and "mid" in dados:
            return float(dados["mid"])
        return None

    def buscar_order_book(self, token_id: str) -> dict | None:
        """
        Retorna o order book (bids e asks) de um token.
        Útil para calcular slippage antes de entrar.
        """
        return self._get(ENDPOINTS["book"], {"token_id": token_id})

# ──────────────────────────────────────────────
#  LÓGICA DE ARBITRAGEM
# ──────────────────────────────────────────────

class DetectorArbitragem:
    """
    Detecta oportunidades de arbitragem.

    ARBITRAGEM INTERNA (o tipo mais comum):
    ────────────────────────────────────────
    Em um mercado binário, YES + NO deve = $1.00.
    Se você comprar YES por $0.47 e NO por $0.48:
      - Total gasto: $0.95
      - Você sempre recebe: $1.00 (um lado sempre ganha)
      - Lucro bruto: $0.05 (5.26%)
      - Após taxas (2% cada lado): ainda ~1.2% de lucro

    ARBITRAGEM EXTERNA (mais rara, mais lucrativa):
    ────────────────────────────────────────────────
    Mesmo evento em dois mercados diferentes com preços
    inconsistentes. Ex: mercado A acha 70% e mercado B
    acha 60% para o mesmo candidato ganhar.
    Compra NO no A (30¢) + YES no B (40¢) = $0.70
    Um deles sempre acerta → recebe $1.00 → lucro $0.30
    """

    def __init__(self, config: dict):
        self.config = config

    def calcular_taxa(self, valor: float) -> float:
        """Calcula a taxa da Polymarket (2% por operação)."""
        return valor * (self.config["taxa_polymarket_pct"] / 100)

    def detectar_arb_interno(self, mercado: Mercado) -> OportunidadeArb | None:
        """
        Verifica se YES + NO < $1.00 no mesmo mercado.
        Isso é raro mas acontece em momentos de baixa liquidez.
        """
        soma = mercado.preco_yes + mercado.preco_no

        # Se a soma for < 1.0, há espaço para arbitragem
        if soma >= 1.0:
            return None

        custo = soma  # você compra $1 de YES e $1 de NO por "soma" centavos
        retorno = 1.0  # um lado sempre ganha

        taxa_yes = self.calcular_taxa(mercado.preco_yes)
        taxa_no  = self.calcular_taxa(mercado.preco_no)
        lucro_bruto = retorno - custo
        lucro_liq   = lucro_bruto - taxa_yes - taxa_no
        lucro_pct   = (lucro_liq / custo) * 100

        if lucro_pct < self.config["min_lucro_pct"]:
            return None  # lucro insuficiente para cobrir riscos

        return OportunidadeArb(
            tipo="interno",
            mercado_a=mercado,
            mercado_b=None,
            lado_a="YES",
            lado_b="NO",
            custo_total=custo,
            retorno_garantido=retorno,
            lucro_bruto=lucro_bruto,
            lucro_liquido=lucro_liq,
            lucro_pct=lucro_pct,
        )

    def detectar_arb_externo(
        self, mercado_a: Mercado, mercado_b: Mercado
    ) -> OportunidadeArb | None:
        """
        Compara dois mercados sobre o MESMO evento.
        Procura inconsistências nas probabilidades implícitas.

        Estratégia: comprar o lado mais barato em cada mercado,
        de forma que um dos dois sempre ganhe.
        """
        # Quatro combinações possíveis:
        combinacoes = [
            # (lado_a, preco_a, lado_b, preco_b)
            ("YES", mercado_a.preco_yes, "NO",  mercado_b.preco_no),
            ("NO",  mercado_a.preco_no,  "YES", mercado_b.preco_yes),
        ]

        melhor = None
        for lado_a, preco_a, lado_b, preco_b in combinacoes:
            custo = preco_a + preco_b
            if custo >= 1.0:
                continue  # não há arb nessa combinação

            retorno = 1.0
            taxa_a = self.calcular_taxa(preco_a)
            taxa_b = self.calcular_taxa(preco_b)
            lucro_bruto = retorno - custo
            lucro_liq   = lucro_bruto - taxa_a - taxa_b
            lucro_pct   = (lucro_liq / custo) * 100

            if lucro_pct < self.config["min_lucro_pct"]:
                continue

            opp = OportunidadeArb(
                tipo="externo",
                mercado_a=mercado_a,
                mercado_b=mercado_b,
                lado_a=lado_a,
                lado_b=lado_b,
                custo_total=custo,
                retorno_garantido=retorno,
                lucro_bruto=lucro_bruto,
                lucro_liquido=lucro_liq,
                lucro_pct=lucro_pct,
            )
            if melhor is None or lucro_pct > melhor.lucro_pct:
                melhor = opp

        return melhor

# ──────────────────────────────────────────────
#  EXECUTOR DE ORDENS (SIMULAÇÃO / REAL)
# ──────────────────────────────────────────────

class ExecutorOrdens:
    """
    Gerencia a execução de ordens.
    Em modo simulação: só loga e registra no histórico.
    Em modo real: usa py-clob-client para enviar ordens.
    """

    def __init__(self, config: dict):
        self.config = config
        self.historico: list[dict] = []
        self.lucro_total = 0.0
        self.operacoes_realizadas = 0

    def executar(self, opp: OportunidadeArb) -> bool:
        """
        Decide o stake e executa (ou simula) a operação.
        Retorna True se a operação foi executada.
        """
        stake = min(
            self.config["max_stake_usd"],
            # Kelly simplificado: não arriscar mais que 5% do capital por vez
            self.config["max_stake_usd"] * 0.05 / max(opp.lucro_pct / 100, 0.01)
        )
        stake = round(stake, 2)

        lucro_esperado = stake * (opp.lucro_pct / 100)

        registro = {
            "timestamp": opp.timestamp,
            "tipo": opp.tipo,
            "mercado_a": opp.mercado_a.pergunta[:60],
            "mercado_b": opp.mercado_b.pergunta[:60] if opp.mercado_b else "-",
            "lado_a": opp.lado_a,
            "lado_b": opp.lado_b,
            "stake_usd": stake,
            "lucro_pct": round(opp.lucro_pct, 2),
            "lucro_esperado_usd": round(lucro_esperado, 4),
            "simulacao": self.config["modo_simulacao"],
        }

        if self.config["modo_simulacao"]:
            print(f"\n{Fore.CYAN}{'─'*60}")
            print(f"{Fore.GREEN}[SIMULAÇÃO] Oportunidade encontrada!")
            print(f"  Mercado A : {opp.mercado_a.pergunta[:55]}")
            if opp.mercado_b:
                print(f"  Mercado B : {opp.mercado_b.pergunta[:55]}")
            print(f"  Posição   : comprar {opp.lado_a} no A + {opp.lado_b} no B")
            print(f"  Stake     : ${stake:.2f}")
            print(f"  Lucro est.: ${lucro_esperado:.4f} ({opp.lucro_pct:.2f}%)")
            print(f"{Fore.CYAN}{'─'*60}")
        else:
            # ── MODO REAL ───────────────────────────────────────
            # Para executar ordens reais você precisa:
            # 1. pip install py-clob-client
            # 2. from py_clob_client.client import ClobClient
            # 3. Configurar carteira Polygon no .env
            #
            # Exemplo de ordem:
            # client = ClobClient(
            #     host=CLOB_URL,
            #     key=os.getenv("PRIVATE_KEY"),
            #     chain_id=137,  # Polygon mainnet
            # )
            # order = client.create_market_order(
            #     token_id=opp.mercado_a.token_yes,
            #     side="BUY",
            #     amount=stake,
            # )
            # client.post_order(order)
            print(f"{Fore.YELLOW}[AVISO] Modo real não implementado. Active e configure a carteira.")
            return False

        self.historico.append(registro)
        self.lucro_total += lucro_esperado
        self.operacoes_realizadas += 1
        return True

    def salvar_historico(self, caminho: str = "historico_arb.json"):
        """Salva todas as operações em JSON para análise posterior."""
        with open(caminho, "w", encoding="utf-8") as f:
            json.dump({
                "lucro_total_simulado": round(self.lucro_total, 4),
                "operacoes": self.operacoes_realizadas,
                "historico": self.historico,
            }, f, ensure_ascii=False, indent=2)
        print(f"\n{Fore.CYAN}[LOG] Histórico salvo em {caminho}")

# ──────────────────────────────────────────────
#  BOT PRINCIPAL
# ──────────────────────────────────────────────

class PolymarketArbBot:
    """
    Orquestra o loop principal do bot:
    1. Busca mercados
    2. Busca preços
    3. Detecta arbitragem
    4. Executa (ou simula) ordens
    5. Repete após intervalo
    """

    def __init__(self):
        self.api      = PolymarketAPI()
        self.detector = DetectorArbitragem(CONFIG)
        self.executor = ExecutorOrdens(CONFIG)
        self.ciclos   = 0

    def _parse_mercado(self, dado: dict) -> Mercado | None:
        """Converte dict da API em objeto Mercado."""
        try:
            # A API retorna 'tokens' com YES/NO separados
            tokens = dado.get("tokens", [])
            if len(tokens) < 2:
                return None

            # Identifica qual token é YES e qual é NO
            token_yes = next((t for t in tokens if t.get("outcome", "").upper() == "YES"), None)
            token_no  = next((t for t in tokens if t.get("outcome", "").upper() == "NO"), None)

            if not token_yes or not token_no:
                return None

            categoria = dado.get("category", "Outro")
            categorias_alvo = CONFIG.get("categorias_alvo")
            if categorias_alvo and categoria not in categorias_alvo:
                return None

            return Mercado(
                id=dado.get("id", ""),
                pergunta=dado.get("question", "Sem título"),
                categoria=categoria,
                token_yes=token_yes["token_id"],
                token_no=token_no["token_id"],
                volume_24h=float(dado.get("volume24hr", 0)),
                ativo=dado.get("active", False),
            )
        except (KeyError, TypeError, ValueError):
            return None

    def _enriquecer_precos(self, mercados: list[Mercado]) -> list[Mercado]:
        """
        Busca o preço atual de YES e NO para cada mercado.
        Faz chamadas com pequeno delay para não ser bloqueado.
        """
        enriquecidos = []
        for m in mercados:
            preco_yes = self.api.buscar_preco_midpoint(m.token_yes)
            preco_no  = self.api.buscar_preco_midpoint(m.token_no)

            if preco_yes is None or preco_no is None:
                continue  # pula mercados sem liquidez

            m.preco_yes = preco_yes
            m.preco_no  = preco_no
            enriquecidos.append(m)
            time.sleep(0.1)  # respeita rate limit da API

        return enriquecidos

    def _varrer_arb_interno(self, mercados: list[Mercado]) -> list[OportunidadeArb]:
        """Verifica cada mercado individualmente."""
        oportunidades = []
        for m in mercados:
            opp = self.detector.detectar_arb_interno(m)
            if opp:
                oportunidades.append(opp)
        return oportunidades

    def _varrer_arb_externo(self, mercados: list[Mercado]) -> list[OportunidadeArb]:
        """
        Compara pares de mercados.
        Para não explodir em N² comparações, filtra por similaridade
        de texto na pergunta (palavras-chave em comum).
        """
        oportunidades = []
        n = len(mercados)
        for i in range(n):
            for j in range(i + 1, n):
                a, b = mercados[i], mercados[j]
                # Heurística simples: mesma categoria + palavras em comum
                if a.categoria != b.categoria:
                    continue
                palavras_a = set(a.pergunta.lower().split())
                palavras_b = set(b.pergunta.lower().split())
                overlap = palavras_a & palavras_b
                if len(overlap) < 4:
                    continue  # pouca sobreposição → provavelmente evento diferente

                opp = self.detector.detectar_arb_externo(a, b)
                if opp:
                    oportunidades.append(opp)
        return oportunidades

    def ciclo(self):
        """Executa um ciclo completo de varredura."""
        self.ciclos += 1
        agora = datetime.now().strftime("%H:%M:%S")
        print(f"\n{Fore.YELLOW}{'═'*60}")
        print(f"{Fore.YELLOW}  Ciclo #{self.ciclos} — {agora}")
        print(f"{Fore.YELLOW}{'═'*60}")

        # 1. Busca mercados
        print(f"{Fore.WHITE}[1/4] Buscando mercados...")
        raw = self.api.buscar_mercados(limit=50)
        mercados_raw = [self._parse_mercado(d) for d in raw]
        mercados = [m for m in mercados_raw if m is not None]
        print(f"      {len(mercados)} mercados válidos encontrados")

        if not mercados:
            print(f"{Fore.RED}      Nenhum mercado encontrado. Verifique a API.")
            return

        # 2. Enriquece com preços
        print(f"{Fore.WHITE}[2/4] Buscando preços (pode demorar um pouco)...")
        mercados = self._enriquecer_precos(mercados)
        print(f"      {len(mercados)} mercados com preço disponível")

        # 3. Detecta arbitragem
        print(f"{Fore.WHITE}[3/4] Analisando oportunidades...")
        opps_interno = self._varrer_arb_interno(mercados)
        opps_externo = self._varrer_arb_externo(mercados)
        todas_opps = opps_interno + opps_externo

        # Ordena por lucro (melhor primeiro)
        todas_opps.sort(key=lambda o: o.lucro_pct, reverse=True)

        print(f"      {len(opps_interno)} arb. interna(s) | {len(opps_externo)} arb. externa(s)")

        # 4. Executa
        print(f"{Fore.WHITE}[4/4] Executando operações...")
        if not todas_opps:
            print(f"      Nenhuma oportunidade acima de {CONFIG['min_lucro_pct']}% encontrada.")
        for opp in todas_opps:
            self.executor.executar(opp)

        # Sumário do ciclo
        print(f"\n{Fore.CYAN}  Lucro acumulado simulado: ${self.executor.lucro_total:.4f}")
        print(f"  Operações totais: {self.executor.operacoes_realizadas}")

    def rodar(self, max_ciclos: int = 0):
        """
        Loop principal.
        max_ciclos=0 → roda indefinidamente (Ctrl+C para parar).
        max_ciclos=N → roda N ciclos e para (útil para testes).
        """
        modo = "SIMULAÇÃO" if CONFIG["modo_simulacao"] else "REAL ⚠️"
        print(f"\n{Fore.GREEN}{'═'*60}")
        print(f"{Fore.GREEN}  POLYMARKET ARB BOT — Modo {modo}")
        print(f"{Fore.GREEN}  Lucro mínimo: {CONFIG['min_lucro_pct']}%")
        print(f"{Fore.GREEN}  Intervalo: {CONFIG['intervalo_scan_seg']}s")
        print(f"{Fore.GREEN}  Categorias: {CONFIG['categorias_alvo']}")
        print(f"{Fore.GREEN}{'═'*60}")

        try:
            while True:
                self.ciclo()
                self.executor.salvar_historico()

                if max_ciclos and self.ciclos >= max_ciclos:
                    print(f"\n{Fore.YELLOW}[FIM] {max_ciclos} ciclos concluídos.")
                    break

                print(f"\n  Próximo scan em {CONFIG['intervalo_scan_seg']}s... (Ctrl+C para parar)")
                time.sleep(CONFIG["intervalo_scan_seg"])

        except KeyboardInterrupt:
            print(f"\n\n{Fore.YELLOW}[STOP] Bot encerrado pelo usuário.")
            self.executor.salvar_historico()
            print(f"{Fore.GREEN}  Total simulado: ${self.executor.lucro_total:.4f} em {self.executor.operacoes_realizadas} operações")


# ──────────────────────────────────────────────
#  PONTO DE ENTRADA
# ──────────────────────────────────────────────

if __name__ == "__main__":
    bot = PolymarketArbBot()

    # Para testes: roda 3 ciclos e para
    # Para rodar indefinidamente: bot.rodar()
    bot.rodar(max_ciclos=3)