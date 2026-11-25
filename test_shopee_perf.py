"""
test_shopee_perf.py — Suite de testes comparativos de performance para a Shopee Affiliate API.

Compara implementação ATUAL vs OTIMIZAÇÕES PROPOSTAS:

  Teste 1 — productCatId/matchId (1 chamada) vs keyword (N chamadas) por categoria
            + verifica VARIEDADE (prefixos distintos) para não trocar velocidade por mesmice
  Teste 2 — Harvest paralelo (ThreadPoolExecutor) vs sequencial
  Teste 4 — Retry + timeout 8s vs 20s sem retry (+ valida se 8s é seguro via p95)
  Teste 5 — requests.Session (reutiliza TLS) vs requests.post (conexão nova)

⚠️  ATENÇÃO
  - Faz chamadas REAIS à API da Shopee (consome quota diária).
  - Estimativa: ~80 chamadas no modo normal, ~40 no --quick.
  - NÃO grava nada em banco/fila. Só leituras.
  - Se sua quota for baixa, use --quick.

Uso:
    python test_shopee_perf.py             # todos os testes
    python test_shopee_perf.py 1 4 5       # só testes 1, 4 e 5
    python test_shopee_perf.py --quick     # metade das chamadas (mais rápido)
    python test_shopee_perf.py 1 --quick   # testa só o 1 no modo rápido

Requer no .env:
    SHOPEE_APP_ID=...
    SHOPEE_APP_SECRET=...
"""

import hashlib
import json
import logging
import os
import random
import re
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter

load_dotenv()

SHOPEE_URL = "https://open-api.affiliate.shopee.com.br/graphql"

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────
#  Classe de teste
# ─────────────────────────────────────────────────────────────────────────
class ShopeePerfTest:
    """Executa bateria comparativa entre implementação atual e otimizada."""

    # ── Configuração base (modo normal) ──
    TEST1_TERMS = ["Vestidos", "Blusas", "Cropped", "Saias"]
    TEST2_TERMS = ["Vestidos", "Blusas", "Saias", "Shorts", "Jeans", "Cropped"]
    TEST2_WORKERS = 3
    TEST2_ROUNDS = 2
    TEST4_LATENCY_CALLS = 10
    TEST5_CALLS = 8

    # Categoria de teste (Roupas Femininas — catid conhecido)
    TEST_CATID = 11059998
    TEST_CATID_LABEL = "Roupas Femininas"

    # Parâmetro para forçar timeout no teste de retry
    TEST4B_FORCED_TIMEOUT = 0.3

    # Pausa entre chamadas (evita rate-limit)
    SLEEP_BETWEEN_CALLS = 0.4

    def __init__(self, app_id: str, app_secret: str, quick: bool = False):
        self.app_id = (app_id or "").strip()
        self.app_secret = (app_secret or "").strip()
        self.quick = quick
        self.session = self._build_session()
        self.results: list[dict] = []

        # Ajusta contagens para modo --quick
        if quick:
            self.TEST1_TERMS = self.TEST1_TERMS[:3]
            self.TEST2_TERMS = self.TEST2_TERMS[:4]
            self.TEST2_ROUNDS = 1
            # [FIX #3] p95 precisa de amostra mínima — nunca abaixo de 10
            self.TEST4_LATENCY_CALLS = max(10, self.TEST4_LATENCY_CALLS)
            self.TEST5_CALLS = 4

    # ─── low-level helpers ────────────────────────────────────────────
    def _build_session(self) -> requests.Session:
        s = requests.Session()
        adapter = HTTPAdapter(pool_connections=5, pool_maxsize=10, max_retries=0)
        s.mount("https://", adapter)
        s.mount("http://", adapter)
        return s

    def _sign(self, payload: str, timestamp: str) -> str:
        factor = f"{self.app_id}{timestamp}{payload}{self.app_secret}"
        return hashlib.sha256(factor.encode()).hexdigest()

    def _headers(self, payload_str: str) -> dict:
        ts = str(int(time.time()))
        sig = self._sign(payload_str, ts)
        return {
            "Authorization": (
                f"SHA256 Credential={self.app_id}, " f"Timestamp={ts}, Signature={sig}"
            ),
            "Content-Type": "application/json",
        }

    def _call_baseline(
        self, query_str: str, timeout: float = 20.0
    ) -> tuple[float, dict, str | None]:
        """Implementação ATUAL: requests.post, timeout 20s, sem retry."""
        payload = json.dumps({"query": query_str})
        headers = self._headers(payload)
        t0 = time.perf_counter()
        try:
            r = requests.post(
                SHOPEE_URL, data=payload.encode(), headers=headers, timeout=timeout
            )
            r.raise_for_status()
            dt = time.perf_counter() - t0
            data = r.json()
            if "errors" in data:
                return dt, {}, str(data["errors"])[:120]
            return dt, data.get("data", {}), None
        except Exception as e:
            return time.perf_counter() - t0, {}, f"{type(e).__name__}: {e}"

    def _call_optimized(
        self,
        query_str: str,
        timeout: float = 8.0,
        max_retries: int = 2,
        session: requests.Session | None = None,
    ) -> tuple[float, dict, str | None]:
        """OTIMIZADO: Session + timeout 8s + retry backoff em 10000/Timeout/429/5xx."""
        sess = session or self.session
        payload = json.dumps({"query": query_str})
        cumulative = 0.0

        for attempt in range(max_retries + 1):
            headers = self._headers(payload)
            t0 = time.perf_counter()
            try:
                r = sess.post(
                    SHOPEE_URL,
                    data=payload.encode(),
                    headers=headers,
                    timeout=timeout,
                )
                r.raise_for_status()
                cumulative += time.perf_counter() - t0
                data = r.json()
                if "errors" in data:
                    err = (data["errors"] or [{}])[0]
                    code = (err.get("extensions") or {}).get("code")
                    if code == 10000 and attempt < max_retries:
                        wait = (2**attempt) * 0.6 + random.uniform(0, 0.4)
                        logger.warning(
                            f"Shopee 10000 — retry {attempt+1}/{max_retries} em {wait:.1f}s"
                        )
                        time.sleep(wait)
                        continue
                    return cumulative, {}, str(data["errors"])[:120]
                return cumulative, data.get("data", {}), None

            # [FIX #4] Retry em 429 / 5xx — casos comuns em produção
            except requests.exceptions.HTTPError as e:
                cumulative += time.perf_counter() - t0
                status = getattr(e.response, "status_code", None)
                if status in (429, 500, 502, 503, 504) and attempt < max_retries:
                    wait = (2**attempt) * 1.0 + random.uniform(0, 0.5)
                    logger.warning(
                        f"HTTP {status} — retry {attempt+1}/{max_retries} em {wait:.1f}s"
                    )
                    time.sleep(wait)
                    continue
                return cumulative, {}, f"HTTP {status}: {str(e)[:80]}"

            except requests.exceptions.Timeout:
                cumulative += time.perf_counter() - t0
                if attempt < max_retries:
                    time.sleep((2**attempt) * 0.5)
                    continue
                return cumulative, {}, "Timeout"

            except Exception as e:
                return (
                    cumulative + (time.perf_counter() - t0),
                    {},
                    f"{type(e).__name__}: {e}",
                )
        return cumulative, {}, "retries exhausted"

    @staticmethod
    def _extract_nodes(data: dict) -> list[dict]:
        if not isinstance(data, dict):
            return []
        po = data.get("productOfferV2") or {}
        return po.get("nodes") or []

    # [FIX #1] Mede diversidade real dos itens para não trocar velocidade por mesmice
    @staticmethod
    def _variety_stats(items: dict[int, str]) -> dict:
        """Mede diversidade dos títulos: tipos distintos, prefixos distintos, ratio."""
        if not items:
            return {"unique_prefixes": 0, "unique_types": 0, "ratio": 0.0}
        stop = {
            "kit",
            "par",
            "jogo",
            "combo",
            "pack",
            "caixa",
            "super",
            "mini",
            "novo",
            "nova",
            "original",
            "promo",
            "oferta",
            "unissex",
            "feminino",
            "masculino",
            "com",
            "para",
            "por",
        }
        prefixes: set[str] = set()
        types: set[str] = set()
        for title in items.values():
            words = re.findall(r"[A-Za-zÀ-ÿ]{3,}", (title or "").lower())
            for w in words:
                if w in stop:
                    continue
                prefixes.add(w)
                types.add(w[:-1] if w.endswith("s") and len(w) > 4 else w)
                break
        return {
            "unique_prefixes": len(prefixes),
            "unique_types": len(types),
            "ratio": len(prefixes) / max(1, len(items)),
        }

    # ─── queries ──────────────────────────────────────────────────────
    def _q_simple(self) -> str:
        return """query {
            productOfferV2(listType: 2, page: 1, limit: 10) {
                nodes { itemId productName }
            }
        }"""

    def _q_keyword(self, term: str, page: int = 1, limit: int = 50) -> str:
        safe_kw = json.dumps(term)
        return f"""query {{
            productOfferV2(keyword: {safe_kw}, sortType: 2, page: {page}, limit: {limit}) {{
                nodes {{ itemId productName priceMin sales }}
            }}
        }}"""

    # [FIX #2] Aceita params_str completo para permitir testar combinações
    def _q_category(self, params_str: str, limit: int = 50) -> str:
        return f"""query {{
            productOfferV2({params_str}, page: 1, limit: {limit}) {{
                nodes {{ itemId productName priceMin sales }}
                pageInfo {{ page hasNextPage }}
            }}
        }}"""

    # ─── TESTE 1 — catid vs keyword (com variedade) ───────────────────
    def test_1_category_endpoint(self):
        print("\n" + "=" * 72)
        print("TESTE 1 — productCatId/matchId (1 chamada) vs keyword (N chamadas)")
        print("=" * 72)
        print(f"Categoria: {self.TEST_CATID_LABEL} (catid={self.TEST_CATID})")
        print(f"Termos baseline: {self.TEST1_TERMS}")

        # BASELINE: keyword × N
        print("\n▶ BASELINE — keyword (uma chamada por subtermo)")
        t0 = time.perf_counter()
        baseline_items: dict[int, str] = {}
        baseline_fail = 0
        for term in self.TEST1_TERMS:
            dt, data, err = self._call_baseline(self._q_keyword(term, page=1, limit=50))
            nodes = self._extract_nodes(data)
            for n in nodes:
                iid = n.get("itemId")
                if iid:
                    baseline_items[iid] = n.get("productName", "")
            status = f"{len(nodes)} itens" if not err else f"ERRO: {err[:60]}"
            print(f"   '{term}': {dt:.2f}s — {status}")
            if err:
                baseline_fail += 1
            time.sleep(self.SLEEP_BETWEEN_CALLS)
        baseline_time = time.perf_counter() - t0
        var_b = self._variety_stats(baseline_items)
        print(
            f"   → Total: {baseline_time:.2f}s | {len(baseline_items)} únicos "
            f"| {baseline_fail} falhas"
        )
        print(
            f"   → Variedade: {var_b['unique_prefixes']} prefixos distintos "
            f"(ratio {var_b['ratio']:.2f})"
        )

        # OTIMIZADO: tenta variantes de nome + combinação com listType
        print("\n▶ OTIMIZADO — 1 chamada com catid (testando variantes de param)")
        variants_to_try = [
            # [FIX #2] ordem: mais provável primeiro; depois as que combinam com listType
            ("matchId", f"matchId: {self.TEST_CATID}, sortType: 2"),
            ("productCatId", f"productCatId: {self.TEST_CATID}, sortType: 2"),
            ("catId", f"catId: {self.TEST_CATID}, sortType: 2"),
            (
                "productCatId+listType",
                f"listType: 2, productCatId: {self.TEST_CATID}, sortType: 2",
            ),
            (
                "matchId+listType",
                f"listType: 2, matchId: {self.TEST_CATID}, sortType: 2",
            ),
        ]

        working_variant = None
        opt_time = 0.0
        opt_items: dict[int, str] = {}
        for variant_name, params in variants_to_try:
            dt, data, err = self._call_optimized(self._q_category(params), timeout=10.0)
            nodes = self._extract_nodes(data)
            status = f"{len(nodes)} itens" if not err else f"ERRO: {err[:80]}"
            print(f"   {variant_name}: {dt:.2f}s — {status}")
            if nodes:
                working_variant = variant_name
                opt_time = dt
                opt_items = {
                    n.get("itemId"): n.get("productName", "")
                    for n in nodes
                    if n.get("itemId")
                }
                break
            time.sleep(self.SLEEP_BETWEEN_CALLS)

        if not working_variant:
            print(
                "\n   ⚠️  Nenhuma variante retornou itens — Otimização 1 NÃO aplicável."
            )
            self.results.append({"test": 1, "applied": False})
            return

        overlap = set(baseline_items) & set(opt_items)
        speedup = baseline_time / max(opt_time, 0.01)
        var_o = self._variety_stats(opt_items)

        print(f"\n   ✅ Variante funcional: '{working_variant}'")
        print(
            f"   Baseline (keyword × {len(self.TEST1_TERMS)}): "
            f"{baseline_time:.2f}s → {len(baseline_items)} itens"
        )
        print(
            f"   Otimizado ({working_variant} × 1): {opt_time:.2f}s "
            f"→ {len(opt_items)} itens"
        )
        print(f"   Overlap: {len(overlap)} itens em comum")
        print(f"   Speedup: {speedup:.1f}x")

        # [FIX #1] Checagem de variedade
        print(
            f"\n   VARIEDADE baseline: {var_b['unique_prefixes']}/{len(baseline_items)} "
            f"prefixos distintos (ratio {var_b['ratio']:.2f})"
        )
        print(
            f"   VARIEDADE otimizado: {var_o['unique_prefixes']}/{len(opt_items)} "
            f"prefixos distintos (ratio {var_o['ratio']:.2f})"
        )
        var_drop = var_b["ratio"] > 0 and var_o["ratio"] < var_b["ratio"] * 0.6
        abs_increase = var_o["unique_prefixes"] >= var_b["unique_prefixes"]
        if var_drop and not abs_increase:
            print(
                "   ⚠️  ALERTA: velocidade sobe mas VARIEDADE cai >40% e o "
                "número absoluto de tipos NÃO aumentou — pode não valer a pena."
            )
        elif var_drop and abs_increase:
            print(
                "   ⚠️  Nota: ratio de variedade cai, mas o NÚMERO ABSOLUTO "
                "de tipos distintos aumentou (mais opções para escolher)."
            )
        else:
            print("   ✅ Variedade preservada.")

        self.results.append(
            {
                "test": 1,
                "applied": True,
                "variant": working_variant,
                "baseline_time": baseline_time,
                "opt_time": opt_time,
                "baseline_items": len(baseline_items),
                "opt_items": len(opt_items),
                "overlap": len(overlap),
                "speedup": speedup,
                "variety_base_ratio": var_b["ratio"],
                "variety_opt_ratio": var_o["ratio"],
                "variety_base_abs": var_b["unique_prefixes"],
                "variety_opt_abs": var_o["unique_prefixes"],
            }
        )

    # ─── TESTE 2 — paralelismo ────────────────────────────────────────
    def test_2_parallel(self):
        # [FIX #5] Speedup medido é CONSERVADOR (baseline sem Session).
        # Em produção (Session + paralelo) o ganho tende a ser AINDA MAIOR.
        print("\n" + "=" * 72)
        print("TESTE 2 — Harvest paralelo (ThreadPoolExecutor) vs sequencial")
        print("=" * 72)
        print(f"Termos: {self.TEST2_TERMS}")
        print(f"Workers: {self.TEST2_WORKERS} | Rodadas: {self.TEST2_ROUNDS}")
        print(
            "Nota: speedup é CONSERVADOR (baseline sem Session). "
            "Ganho real em produção tende a ser maior."
        )

        seq_times, par_times = [], []
        seq_items, par_items = 0, 0

        for r in range(self.TEST2_ROUNDS):
            # Sequencial
            t0 = time.perf_counter()
            for term in self.TEST2_TERMS:
                _, data, _ = self._call_baseline(
                    self._q_keyword(term, page=1, limit=50)
                )
                seq_items += len(self._extract_nodes(data))
                time.sleep(0.2)
            seq_times.append(time.perf_counter() - t0)
            time.sleep(1.0)

            # Paralelo
            t0 = time.perf_counter()
            with ThreadPoolExecutor(max_workers=self.TEST2_WORKERS) as pool:
                futures = [
                    pool.submit(
                        self._call_baseline,
                        self._q_keyword(term, page=1, limit=50),
                    )
                    for term in self.TEST2_TERMS
                ]
                for f in as_completed(futures):
                    try:
                        _, data, _ = f.result()
                        par_items += len(self._extract_nodes(data))
                    except Exception:
                        pass
            par_times.append(time.perf_counter() - t0)
            time.sleep(1.0)

        avg_seq = statistics.mean(seq_times)
        avg_par = statistics.mean(par_times)
        speedup = avg_seq / max(avg_par, 0.01)

        print(
            f"\n   Sequencial: {avg_seq:.2f}s "
            f"(rodadas: {[f'{t:.2f}' for t in seq_times]})"
        )
        print(
            f"   Paralelo ({self.TEST2_WORKERS}w): {avg_par:.2f}s "
            f"(rodadas: {[f'{t:.2f}' for t in par_times]})"
        )
        print(f"   Speedup: {speedup:.2f}x")
        print(f"   Itens totais: seq={seq_items} | par={par_items}")

        self.results.append(
            {
                "test": 2,
                "seq_time": avg_seq,
                "par_time": avg_par,
                "speedup": speedup,
                "workers": self.TEST2_WORKERS,
            }
        )

    # ─── TESTE 4 — retry + timeout ────────────────────────────────────
    def test_4_retry_timeout(self):
        print("\n" + "=" * 72)
        print("TESTE 4 — Retry + timeout 8s vs 20s sem retry")
        print("=" * 72)

        q = self._q_simple()

        # 4a — distribuição de latência real
        print(
            f"\n▶ 4a — Latência real ({self.TEST4_LATENCY_CALLS} chamadas, "
            f"para validar 8s)"
        )
        lats: list[float] = []
        fails = 0
        for _ in range(self.TEST4_LATENCY_CALLS):
            dt, _, err = self._call_baseline(q, timeout=20.0)
            if err:
                fails += 1
            else:
                lats.append(dt)
            time.sleep(self.SLEEP_BETWEEN_CALLS)

        if not lats:
            print("   ❌ Todas as chamadas falharam — impossível medir latência.")
            self.results.append({"test": 4, "error": "all calls failed"})
            return

        srt = sorted(lats)
        p50 = srt[len(srt) // 2]
        p95 = srt[min(len(srt) - 1, int(len(srt) * 0.95))]
        mx = max(lats)
        print(
            f"   {len(lats)} ok / {fails} falhas | p50={p50:.2f}s "
            f"| p95={p95:.2f}s | max={mx:.2f}s"
        )
        if p95 >= 8.0:
            print(f"   ⚠️  p95 ({p95:.2f}s) ≥ 8s → timeout de 8s pode ser AGRESSIVO.")
        else:
            print(f"   ✅ p95 ({p95:.2f}s) < 8s → timeout de 8s é SEGURO.")

        # 4b — mecanismo de retry (forçando timeout)
        print(
            f"\n▶ 4b — Mecanismo de retry "
            f"(timeout forçado em {self.TEST4B_FORCED_TIMEOUT}s)"
        )
        dt_b, _, err_b = self._call_baseline(q, timeout=self.TEST4B_FORCED_TIMEOUT)
        dt_o, _, err_o = self._call_optimized(
            q, timeout=self.TEST4B_FORCED_TIMEOUT, max_retries=2
        )
        print(
            f"   Baseline (sem retry): {dt_b:.2f}s → " f"{'FALHOU' if err_b else 'OK'}"
        )
        print(
            f"   Otimizado (retry 2x): {dt_o:.2f}s → " f"{'FALHOU' if err_o else 'OK'}"
        )
        overhead = dt_o - dt_b
        retry_ativo = dt_o > dt_b * 1.5
        print(f"   Overhead do retry em falha persistente: +{overhead:.2f}s")
        print(
            f"   → Mecanismo de retry "
            f"{'✅ ativou' if retry_ativo else '❌ NÃO ativou'} "
            f"(overhead esperado > 1.5x)"
        )

        self.results.append(
            {
                "test": 4,
                "p50": p50,
                "p95": p95,
                "max": mx,
                "timeout_8s_safe": p95 < 8.0,
                "retry_overhead": overhead,
                "retry_ativo": retry_ativo,
            }
        )

    # ─── TESTE 5 — Session vs post ────────────────────────────────────
    def test_5_session(self):
        print("\n" + "=" * 72)
        print("TESTE 5 — requests.Session vs requests.post")
        print("=" * 72)
        q = self._q_simple()
        n = self.TEST5_CALLS

        # Baseline: conexão nova a cada chamada
        print(f"\n▶ BASELINE — requests.post ({n} chamadas, conexão nova)")
        post_ok: list[float] = []
        post_fail = 0
        for _ in range(n):
            dt, _, err = self._call_baseline(q, timeout=20.0)
            if err:
                post_fail += 1
            else:
                post_ok.append(dt)
            time.sleep(0.2)

        # Otimizado: sessão nova (garante que medimos do zero)
        print(f"▶ OTIMIZADO — requests.Session ({n} chamadas, reutilizando TLS)")
        fresh = self._build_session()
        # Warm-up não-contabilizado para mostrar steady-state de produção
        try:
            fresh.get("https://open-api.affiliate.shopee.com.br/", timeout=5)
        except Exception:
            pass
        sess_ok: list[float] = []
        sess_fail = 0
        for _ in range(n):
            dt, _, err = self._call_optimized(
                q, timeout=8.0, max_retries=0, session=fresh
            )
            if err:
                sess_fail += 1
            else:
                sess_ok.append(dt)
            time.sleep(0.2)

        if not post_ok or not sess_ok:
            print("   ❌ Dados insuficientes para comparar.")
            self.results.append({"test": 5, "error": "insufficient data"})
            return

        avg_post = statistics.mean(post_ok)
        avg_sess = statistics.mean(sess_ok)
        gain_ms = (avg_post - avg_sess) * 1000.0
        print(
            f"\n   requests.post:    avg={avg_post*1000:.0f}ms | "
            f"min={min(post_ok)*1000:.0f}ms | max={max(post_ok)*1000:.0f}ms "
            f"| falhas={post_fail}"
        )
        print(
            f"   requests.Session: avg={avg_sess*1000:.0f}ms | "
            f"min={min(sess_ok)*1000:.0f}ms | max={max(sess_ok)*1000:.0f}ms "
            f"| falhas={sess_fail}"
        )
        print(f"   Ganho médio por chamada: {gain_ms:+.0f}ms")

        self.results.append(
            {
                "test": 5,
                "avg_post": avg_post,
                "avg_session": avg_sess,
                "gain_ms": gain_ms,
                "post_fails": post_fail,
                "sess_fails": sess_fail,
            }
        )

    # ─── resumo ───────────────────────────────────────────────────────
    def _print_summary(self):
        print("\n" + "#" * 72)
        print("# RESUMO")
        print("#" * 72)
        if not self.results:
            print("Nenhum resultado coletado.")
            return

        for r in self.results:
            t = r.get("test")
            if t == 1:
                if not r.get("applied"):
                    print(
                        "Teste 1 (catid): ❌ NÃO APLICÁVEL — "
                        "nenhuma variante retornou itens"
                    )
                else:
                    print(
                        f"Teste 1 ({r['variant']}): {r['baseline_time']:.2f}s → "
                        f"{r['opt_time']:.2f}s | speedup {r['speedup']:.1f}x | "
                        f"itens {r['baseline_items']} → {r['opt_items']} | "
                        f"overlap {r['overlap']}"
                    )
                    print(
                        f"   variedade: baseline ratio "
                        f"{r['variety_base_ratio']:.2f} "
                        f"({r['variety_base_abs']} prefixos) → "
                        f"otimizado ratio {r['variety_opt_ratio']:.2f} "
                        f"({r['variety_opt_abs']} prefixos)"
                    )
            elif t == 2:
                print(
                    f"Teste 2 ({r['workers']} workers): {r['seq_time']:.2f}s → "
                    f"{r['par_time']:.2f}s | speedup {r['speedup']:.2f}x"
                )
            elif t == 4:
                if "error" in r:
                    print(f"Teste 4: ❌ {r['error']}")
                else:
                    print(
                        f"Teste 4 (latência): p50={r['p50']:.2f}s "
                        f"p95={r['p95']:.2f}s max={r['max']:.2f}s | "
                        f"8s seguro={r['timeout_8s_safe']} | "
                        f"overhead retry={r['retry_overhead']:+.2f}s"
                    )
            elif t == 5:
                if "error" in r:
                    print(f"Teste 5: ❌ {r['error']}")
                else:
                    print(
                        f"Teste 5 (session): post={r['avg_post']*1000:.0f}ms "
                        f"session={r['avg_session']*1000:.0f}ms | "
                        f"ganho {r['gain_ms']:+.0f}ms/chamada"
                    )

    def _print_recommendations(self):
        print("\n" + "#" * 72)
        print("# RECOMENDAÇÕES")
        print("#" * 72)
        for r in self.results:
            t = r.get("test")
            if t == 1:
                if not r.get("applied"):
                    print(
                        "❌ Teste 1: NÃO implementar — variantes não suportadas "
                        "ou retorno vazio"
                    )
                    continue
                speedup = r.get("speedup", 0)
                var_b = r.get("variety_base_ratio", 0)
                var_o = r.get("variety_opt_ratio", 0)
                abs_increase = r.get("variety_opt_abs", 0) >= r.get(
                    "variety_base_abs", 0
                )
                ok_vel = speedup >= 1.5
                ok_var = (var_b == 0) or (var_o >= var_b * 0.6) or abs_increase

                if ok_vel and ok_var:
                    print(
                        f"✅ Teste 1: implementar '{r['variant']}' — "
                        f"{speedup:.1f}x mais rápido com variedade OK"
                    )
                elif ok_vel and not ok_var:
                    print(
                        f"⚠️  Teste 1: ganho de {speedup:.1f}x MAS variedade "
                        f"caiu >40% e absoluto não aumentou — testar "
                        f"mitigação (pages 1+2, ou mix catid+keyword)"
                    )
                elif not ok_vel and ok_var:
                    print(
                        f"⚠️  Teste 1: variedade OK mas ganho baixo "
                        f"({speedup:.1f}x) — implementar só se precisar "
                        f"de mais itens por chamada"
                    )
                else:
                    print(
                        f"❌ Teste 1: sem ganho e sem variedade — NÃO "
                        f"implementar como está"
                    )
            elif t == 2:
                if r.get("speedup", 0) >= 1.3:
                    print(
                        f"✅ Teste 2: implementar ThreadPoolExecutor("
                        f"max_workers={r['workers']}) — {r['speedup']:.2f}x"
                        f" (conservador)"
                    )
                else:
                    print(
                        f"⚠️  Teste 2: ganho baixo ({r.get('speedup', 0):.2f}x)"
                        f" — talvez não compense a complexidade"
                    )
            elif t == 4 and "error" not in r:
                if r.get("timeout_8s_safe"):
                    print(
                        "✅ Teste 4: timeout 8s é seguro — implementar "
                        "retry backoff (10000/Timeout/429/5xx)"
                    )
                else:
                    print(
                        f"⚠️  Teste 4: 8s agressivo (p95={r['p95']:.2f}s) — "
                        f"usar timeout ~{max(8, int(r['p95']*1.5))}s"
                    )
            elif t == 5 and "error" not in r:
                if r.get("gain_ms", 0) >= 30:
                    print(
                        f"✅ Teste 5: usar requests.Session — ganho de "
                        f"{r['gain_ms']:.0f}ms/chamada"
                    )
                else:
                    print(
                        f"⚠️  Teste 5: ganho baixo ({r.get('gain_ms', 0):.0f}ms)"
                        f" — opcional"
                    )

    # ─── runner ───────────────────────────────────────────────────────
    def run(self, tests: list[int]):
        print("\n" + "#" * 72)
        print(f"# Shopee Perf Test — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"# Modo: {'quick' if self.quick else 'normal'}")
        print("#" * 72)

        if not self.app_id or not self.app_secret:
            print("\n❌ SHOPEE_APP_ID e SHOPEE_APP_SECRET precisam estar no .env")
            sys.exit(1)

        print("\n🔌 Sanity check de conectividade...")
        dt, data, err = self._call_baseline(self._q_simple(), timeout=10.0)
        if err:
            print(f"❌ Falha na conexão: {err}")
            sys.exit(1)
        nodes = self._extract_nodes(data)
        print(f"✅ Conectado em {dt:.2f}s ({len(nodes)} itens de amostra)")

        dispatch = {
            1: self.test_1_category_endpoint,
            2: self.test_2_parallel,
            4: self.test_4_retry_timeout,
            5: self.test_5_session,
        }

        for t in tests:
            if t not in dispatch:
                print(f"\n⚠️  Teste {t} desconhecido — ignorando.")
                continue
            try:
                dispatch[t]()
            except Exception as e:
                logger.exception(f"Erro no teste {t}: {e}")
            time.sleep(1.5)

        self._print_summary()
        self._print_recommendations()


# ─────────────────────────────────────────────────────────────────────────
#  Entrypoint
# ─────────────────────────────────────────────────────────────────────────
def main():
    args = sys.argv[1:]
    quick = "--quick" in args
    tests = sorted({int(a) for a in args if a.isdigit()})
    if not tests:
        tests = [1, 2, 4, 5]

    t = ShopeePerfTest(
        os.environ.get("SHOPEE_APP_ID", ""),
        os.environ.get("SHOPEE_APP_SECRET", ""),
        quick=quick,
    )
    t.run(tests)


if __name__ == "__main__":
    main()
