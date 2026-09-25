"""Pruebas automáticas: reglas del sistema y de gestión. Se ejecutan en cada cambio (workflow «Pruebas»)."""
import math
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import indicadores as ind  # noqa: E402
import sistema as sis  # noqa: E402


def velas(cierres, rango=0.02):
    D = 86400000
    v = {"t": [], "ct": [], "o": [], "h": [], "l": [], "c": [], "v": []}
    for i, c in enumerate(cierres):
        o = cierres[i - 1] if i else c
        v["t"].append(i * D); v["ct"].append((i + 1) * D - 1); v["o"].append(o)
        v["h"].append(max(o, c) * (1 + rango)); v["l"].append(min(o, c) * (1 - rango)); v["c"].append(c); v["v"].append(1e6)
    return v


class Indicadores(unittest.TestCase):
    def test_sma(self):
        self.assertAlmostEqual(ind.sma([1, 2, 3, 4], 2)[-1], 3.5)

    def test_maximo_previo_excluye_hoy(self):
        self.assertEqual(ind.maximo_previo([1, 5, 2, 9], 2)[-1], 5)


class Reglas(unittest.TestCase):
    def setUp(self):
        self.sube = velas([100 * 1.01 ** i for i in range(260)])
        self.baja = velas([100 * 0.99 ** i for i in range(260)])

    def test_nucleo(self):
        self.assertTrue(sis.nucleo_dentro(self.sube, sis.preparar(self.sube)))
        self.assertFalse(sis.nucleo_dentro(self.baja, sis.preparar(self.baja)))

    def test_ruptura_y_stop(self):
        v = velas([100 * 1.01 ** i for i in range(260)], rango=0.001)   # cierre de hoy > máximo previo
        c = sis.candidatas_satelite({"X": v}, {"X": sis.preparar(v)}, ["X"], set())
        self.assertEqual(len(c), 1)
        self.assertLess(c[0]["stop"], v["c"][-1])

    def test_regimen(self):
        self.assertTrue(sis.regimen_btc(self.sube, sis.preparar(self.sube))["alcista"])
        self.assertFalse(sis.regimen_btc(self.baja, sis.preparar(self.baja))["alcista"])

    def test_trailing_nunca_baja(self):
        s = sis.preparar(self.sube)
        pos = {"stop": 10 ** 9, "maximo": 0}
        self.assertEqual(sis.actualizar_trailing(pos, self.sube, s)["stop"], 10 ** 9)


class Gestion(unittest.TestCase):
    def test_tamano_por_riesgo(self):
        self.assertAlmostEqual(sis.tamano_por_riesgo(0.10, 100, 80), 0.05)   # stop a 20 % -> 5 %
        self.assertEqual(sis.tamano_por_riesgo(0.10, 100, 95), 0.10)        # stop a 5 % -> tamaño completo
        self.assertEqual(sis.tamano_por_riesgo(0.25, 100, None), 0.25)

    def test_precio_limite(self):
        self.assertAlmostEqual(sis.precio_limite(120, 100, 0.10), 101)       # como mucho +1 % sobre el cierre
        self.assertAlmostEqual(sis.precio_limite(130, 100, 0.40), 100)       # tras +40 % en 24 h, el cierre
        self.assertAlmostEqual(sis.precio_limite(99, 100, 0.0), 99)          # si está por debajo, el bid

    def test_proteccion_horaria(self):
        v = {"c": [100] * 24 + [95], "v": [1] * 24 + [3]}
        self.assertTrue(sis.proteccion_horaria(v, 98))
        v["v"][-1] = 1.5
        self.assertFalse(sis.proteccion_horaria(v, 98))

    def test_recomendacion_no_compra_en_bajista(self):
        v = velas([100 * 1.01 ** i for i in range(260)])
        r = sis.recomendacion("X", v, sis.preparar(v), v["c"][-1] * 1.05, None, False, 5)
        self.assertEqual(r["accion"], "NO COMPRAR")
        r = sis.recomendacion("X", v, sis.preparar(v), v["c"][-1] * 1.05, None, True, 5)
        self.assertEqual(r["accion"], "POSIBLE COMPRA")
        self.assertFalse(math.isnan(r["dist"]))


class Volatilidad(unittest.TestCase):
    def test_factor(self):
        tranquila = velas([100 * (1.001 if i % 2 else 1.0) for i in range(60)])     # ±0,1 % diario
        self.assertEqual(sis.factor_volatilidad(tranquila)[0], 1.0)
        import random
        random.seed(3)
        c = [100.0]
        for _ in range(60):
            c.append(c[-1] * math.exp(random.gauss(0, 0.06)))      # ~115 % anual
        f, vol = sis.factor_volatilidad(velas(c))
        self.assertGreater(vol, 0.8)
        self.assertAlmostEqual(f, sis.VOL_OBJETIVO / vol)


if __name__ == "__main__":
    unittest.main()
