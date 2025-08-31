import random
import itertools as it
from collections import Counter
import pandas as pd
from typing import Optional, Tuple, List

NUMBERS = list(range(1, 61))

def _decenio(n:int)->int:
    return (n-1)//10 + 1

def _has_long_sequence(ticket, L=3):
    t = sorted(ticket)
    run = 1
    for a,b in zip(t, t[1:]):
        if b == a + 1:
            run += 1
            if run >= L:
                return True
        else:
            run = 1
    return False

class GameGenerator:
    """
    Gerador de jogos Mega-Sena com:
      - Perfis de geração: "historico" (soma~145), "misto" (soma~183), "alto" (soma~260)
      - Penalidade de soma suave em torno de um alvo configurável (sum_target/sum_weight)
      - Penalidade por desvio de buckets (1–20, 21–40, 41–60) via bucket_target/bucket_weight
      - Regras clássicas (paridade, finais/decênios, sem sequências longas, etc.)
      - Modo balanced=True tenta limitar exposição de cada dezena (1x quando possível)
      - Saída embaralhada (display_shuffle=True) para não parecer “sempre começa baixo”
    """
    def __init__(self,
                 seed: str = "MEGASENA-SEED",
                 # trava rígida de soma (desativada por padrão)
                 sum_band: Optional[Tuple[int,int]] = None,

                 # PERFIL: "historico" | "misto" | "alto"
                 profile: str = "historico",

                 # alvo de soma e peso da penalidade (se None, define pelo perfil)
                 sum_target: Optional[float] = None,
                 sum_weight: Optional[float] = None,

                 # proporções-alvo para buckets (1–20, 21–40, 41–60)
                 bucket_target: Optional[Tuple[float, float, float]] = None,
                 bucket_weight: float = 0.20,

                 # regras base
                 min_over31: int = 3,
                 min_high: int = 1,              # mínimo em 41–60
                 odd_range: Tuple[int,int] = (2,4),
                 max_same_decenio: int = 4,
                 max_same_ending: int = 2,
                 max_mult5: int = 3,
                 max_exposure: int = 4,

                 # população/seleção
                 pop_size: int = 6000,
                 candidates_per_pick: int = 1200,

                 # apresentação
                 display_shuffle: bool = True):

        self.rng = random.Random(seed)
        self.SUM_BAND = sum_band
        self.PROFILE = str(profile).lower().strip()
        self.BUCKET_WEIGHT = float(bucket_weight)

        # Defaults por perfil
        if self.PROFILE == "misto":
            default_sum_target = 183.0
            default_sum_weight = 0.10
            default_bucket_target = (1/3, 1/3, 1/3)
            min_high_default = 1
        elif self.PROFILE == "alto":
            default_sum_target = 260.0
            default_sum_weight = 0.05
            default_bucket_target = (0.15, 0.30, 0.55)  # puxa mais para 41–60
            min_high_default = 3
        else:  # historico
            default_sum_target = 145.0
            default_sum_weight = 0.15
            default_bucket_target = (0.33, 0.33, 0.34)
            min_high_default = 1

        self.SUM_TARGET = float(default_sum_target if sum_target is None else sum_target)
        self.SUM_WEIGHT = float(default_sum_weight if sum_weight is None else sum_weight)
        self.BUCKET_TARGET = tuple(default_bucket_target if bucket_target is None else bucket_target)

        # regras base
        self.MIN_OVER_31 = int(min_over31)
        self.MIN_HIGH = max(int(min_high), min_high_default)  # garante mínimo coerente com o perfil
        self.ODD_RANGE = odd_range
        self.MAX_SAME_DECENIO = int(max_same_decenio)
        self.MAX_SAME_ENDING = int(max_same_ending)
        self.MAX_MULT_5 = int(max_mult5)
        self.MAX_EXPOSURE = int(max_exposure)

        # população/seleção
        self.POP_SIZE = int(pop_size)
        self.CANDIDATES_PER_PICK = int(candidates_per_pick)

        # apresentação
        self.DISPLAY_SHUFFLE = bool(display_shuffle)

    def _ticket_ok(self, t: List[int]) -> bool:
        t = sorted(t)
        s = sum(t)

        # (1) Soma rígida (se definida)
        if self.SUM_BAND is not None:
            if not (self.SUM_BAND[0] <= s <= self.SUM_BAND[1]):
                return False

        # (2) Composição >31
        if sum(1 for x in t if x > 31) < self.MIN_OVER_31:
            return False

        # (3) Altos (41–60)
        if self.MIN_HIGH > 0 and sum(1 for x in t if x >= 41) < self.MIN_HIGH:
            return False

        # (4) Paridade
        odds = sum(1 for x in t if x % 2 == 1)
        if not (self.ODD_RANGE[0] <= odds <= self.ODD_RANGE[1]):
            return False

        # (5) Concentração por decênio e finais
        dec_counts = Counter(_decenio(x) for x in t)
        if any(c > self.MAX_SAME_DECENIO for c in dec_counts.values()):
            return False

        end_counts = Counter(x % 10 for x in t)
        if any(c > self.MAX_SAME_ENDING for c in end_counts.values()):
            return False

        # (6) Múltiplos de 5 em excesso
        if sum(1 for x in t if x % 5 == 0) > self.MAX_MULT_5:
            return False

        # (7) Evitar sequências longas
        if _has_long_sequence(t, 3):
            return False

        return True

    def _anti_popularity_penalty(self, t: List[int]) -> float:
        t = sorted(t)
        pen = 0.0

        # (A) Soma – penalidade suave em torno do alvo
        if self.SUM_WEIGHT > 0:
            pen += self.SUM_WEIGHT * (abs(sum(t) - self.SUM_TARGET) / 60.0)

        # (B) Buckets (1–20, 21–40, 41–60): distância L1 às proporções-alvo
        b1 = sum(1 for x in t if x <= 20)
        b2 = sum(1 for x in t if 21 <= x <= 40)
        b3 = 6 - b1 - b2  # 41–60
        props = (b1/6.0, b2/6.0, b3/6.0)
        bucket_dist = sum(abs(p - q) for p, q in zip(props, self.BUCKET_TARGET))
        pen += self.BUCKET_WEIGHT * bucket_dist

        # (C) Finais repetidos e (D) concentração por decênio
        end_counts = Counter(x % 10 for x in t)
        pen += sum(max(0, c - 1) for c in end_counts.values()) * 0.3

        dec_counts = Counter(_decenio(x) for x in t)
        pen += sum(max(0, c - 2) for c in dec_counts.values()) * 0.4

        # (E) Múltiplos de 5 excessivos
        pen += max(0, sum(1 for x in t if x % 5 == 0) - 2) * 0.5

        return pen

    @staticmethod
    def _pairs_of(t):
        return {tuple(sorted(p)) for p in it.combinations(t, 2)}

    @staticmethod
    def _triples_of(t):
        return {tuple(sorted(p)) for p in it.combinations(t, 3)}

    @staticmethod
    def _jaccard(a, b):
        A, B = set(a), set(b)
        inter = len(A & B)
        union = len(A | B)
        return inter / union if union else 0.0

    def _shuffle_for_display(self, t):
        """Embaralha apenas para exibição — não altera a lógica interna."""
        l = list(t)
        self.rng.shuffle(l)
        return l

    def generate(self, n_games: int = 10, balanced: bool = False):
        """
        Gera n_games jogos.
        - balanced=True: tenta limitar a exposição de cada dezena a 1x quando possível
          (especialmente válido quando n_games*6 <= 60).
        Saída vem **embaralhada** para exibição (se display_shuffle=True).
        """
        # 1) Gera candidatos válidos
        candidates = set()
        tries = 0
        max_tries = self.POP_SIZE * 50
        while len(candidates) < self.POP_SIZE and tries < max_tries:
            tries += 1
            t = tuple(sorted(self.rng.sample(NUMBERS, 6)))
            if self._ticket_ok(t):
                candidates.add(t)
        candidates = list(candidates)

        # 2) Seleção gulosa com cobertura de pares/trincas
        selected: List[Tuple[int, ...]] = []
        covered_pairs = set()
        covered_triples = set()
        exposure = Counter()

        # Limite local de exposição (modo balanced tenta 1x por número)
        local_max_exp = 1 if (balanced and n_games * 6 <= len(NUMBERS)) else self.MAX_EXPOSURE

        def feasible(t):
            return all(exposure[x] < local_max_exp for x in t)

        def score(t):
            p_new = len(self._pairs_of(t) - covered_pairs)
            tr_new = len(self._triples_of(t) - covered_triples)
            overlap_pen = sum(self._jaccard(t, s) for s in selected)
            ap_pen = self._anti_popularity_penalty(t)
            exp_pen = sum((exposure[x] / max(1, local_max_exp)) for x in t) * 0.3
            return (3.0 * p_new) + (1.5 * tr_new) - (2.5 * overlap_pen) - (1.8 * ap_pen) - exp_pen

        for _ in range(n_games):
            if not candidates:
                break
            pool_size = min(self.CANDIDATES_PER_PICK, len(candidates))
            pool = self.rng.sample(candidates, pool_size)

            best, best_score = None, -1e18
            # 1ª passada: respeitando viabilidade (exposição)
            for t in pool:
                if not feasible(t):
                    continue
                sc = score(t)
                if sc > best_score:
                    best_score, best = sc, t

            # 2ª passada (fallback): se nada viável, ignora exposição local
            if best is None:
                for t in pool:
                    sc = score(t)
                    if sc > best_score:
                        best_score, best = sc, t

            if best is None:
                break

            selected.append(best)
            covered_pairs |= self._pairs_of(best)
            covered_triples |= self._triples_of(best)
            for x in best:
                exposure[x] += 1

        # Completa se necessário (ignora balanced para preencher)
        while len(selected) < n_games and candidates:
            pool_size = min(self.CANDIDATES_PER_PICK, len(candidates))
            pool = self.rng.sample(candidates, pool_size)
            best, best_score = None, -1e18
            for t in pool:
                sc = (3.0 * len(self._pairs_of(t) - covered_pairs)
                      + 1.5 * len(self._triples_of(t) - covered_triples)
                      - 2.5 * sum(self._jaccard(t, s) for s in selected)
                      - 1.8 * self._anti_popularity_penalty(t))
                if sc > best_score:
                    best_score, best = sc, t
            if best is None:
                break
            selected.append(best)
            covered_pairs |= self._pairs_of(best)
            covered_triples |= self._triples_of(best)

        # 3) Saída: embaralhada para apresentação (não “começar baixo”)
        if self.DISPLAY_SHUFFLE:
            return [self._shuffle_for_display(t) for t in selected]
        return [sorted(list(t)) for t in selected]

def load_history_numbers_from_excel(path: str) -> pd.DataFrame:
    """
    Lê um Excel contendo resultados históricos da Mega-Sena.
    Parser tolerante: procura 6 inteiros por linha e descarta o restante.
    Retorna DataFrame com colunas ['n1'..'n6'].
    """
    df = pd.read_excel(path, engine="openpyxl")
    cleaned = []
    for _, row in df.iterrows():
        nums = []
        for v in row.values:
            try:
                if isinstance(v, str):
                    v = v.strip()
                    if v.isdigit():
                        v = int(v)
                    else:
                        # extrai números de strings "33 - 44 - 55" etc.
                        for m in __import__("re").findall(r"\d+", v):
                            iv = int(m)
                            if 1 <= iv <= 60:
                                nums.append(iv)
                elif isinstance(v, (int, float)):
                    iv = int(v)
                    if 1 <= iv <= 60:
                        nums.append(iv)
            except Exception:
                continue
        if len(nums) >= 6:
            cleaned.append(sorted(nums[:6]))
    out = pd.DataFrame(cleaned, columns=[f"n{i}" for i in range(1,7)])
    return out
