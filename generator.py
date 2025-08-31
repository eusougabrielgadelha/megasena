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
    def __init__(self,
                 seed: str = "MEGASENA-SEED",
                 sum_band: Optional[Tuple[int,int]] = None,   # <- sem trava rígida por padrão
                 min_over31: int = 3,
                 min_high: int = 1,                           # <- novo: mínimo em 41–60 por jogo
                 odd_range: Tuple[int,int] = (2,4),
                 max_same_decenio: int = 4,
                 max_same_ending: int = 2,
                 max_mult5: int = 3,
                 max_exposure: int = 4,
                 pop_size: int = 6000,
                 candidates_per_pick: int = 1200,
                 display_shuffle: bool = True):
        """
        display_shuffle: se True, embaralha a ordem das dezenas de cada jogo
                         na SAÍDA (só apresentação), para não “começar baixo”.
        """
        self.rng = random.Random(seed)
        self.SUM_BAND = sum_band          # se None, não aplica filtro rígido de soma
        self.MIN_OVER_31 = min_over31
        self.MIN_HIGH = min_high          # pelo menos X números em 41–60
        self.ODD_RANGE = odd_range
        self.MAX_SAME_DECENIO = max_same_decenio
        self.MAX_SAME_ENDING = max_same_ending
        self.MAX_MULT_5 = max_mult5
        self.MAX_EXPOSURE = max_exposure
        self.POP_SIZE = pop_size
        self.CANDIDATES_PER_PICK = candidates_per_pick
        self.DISPLAY_SHUFFLE = display_shuffle

        # alvo “natural” de soma quando não há banda rígida (média de 1..60 = 30,5; *6 = 183)
        self._SOFT_SUM_CENTER = 183.0 if self.SUM_BAND is None else (self.SUM_BAND[0] + self.SUM_BAND[1]) / 2.0

    def _ticket_ok(self, t: List[int]) -> bool:
        t = sorted(t)
        s = sum(t)

        # (1) Soma – opcionalmente rígida
        if self.SUM_BAND is not None:
            if not (self.SUM_BAND[0] <= s <= self.SUM_BAND[1]):
                return False

        # (2) Composição básica
        if sum(1 for x in t if x > 31) < self.MIN_OVER_31:
            return False

        # (3) Exigir presença de altos (41–60)
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

        # (6) Múltiplos de 5 excessivos
        if sum(1 for x in t if x % 5 == 0) > self.MAX_MULT_5:
            return False

        # (7) Evitar sequências longas
        if _has_long_sequence(t, 3):
            return False

        return True

    def _anti_popularity_penalty(self, t: List[int]) -> float:
        t = sorted(t)
        pen = 0.0

        # (A) “Datas”/muitos baixos: 1–20
        low20 = sum(1 for x in t if x <= 20)
        pen += max(0, low20 - 2) * 0.6

        # (B) Poucos altos: incentivar ter altos (41–60)
        high = sum(1 for x in t if x >= 41)
        pen += max(0, 2 - high) * 0.7    # empurra a ter pelo menos 2 altos (suave)

        # (C) Soma – penalidade suave ao redor do centro
        pen += abs(sum(t) - self._SOFT_SUM_CENTER) / 60.0

        # (D) Finais repetidos e concentração por decênios
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
        selected = []
        covered_pairs = set()
        covered_triples = set()
        exposure = Counter()

        # Limite local de exposição (modo balanceado tenta 1x por número)
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

            # 2ª passada (fallback): se nada viável, relaxa a restrição local
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
    Tenta ler um Excel contendo os resultados históricos da Mega-Sena.
    O parser é tolerante: procura 6 inteiros por linha e descarta o restante.
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
                        continue
                if isinstance(v, (int, float)):
                    iv = int(v)
                    if 1 <= iv <= 60:
                        nums.append(iv)
            except Exception:
                continue
        if len(nums) >= 6:
            cleaned.append(sorted(nums[:6]))
    out = pd.DataFrame(cleaned, columns=[f"n{i}" for i in range(1,7)])
    return out
