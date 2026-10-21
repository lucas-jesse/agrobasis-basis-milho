"""
atualizar_basis_milho.py — Job agendado do Basis do Milho (AgroBasis)

Uso:
    python atualizar_basis_milho.py

Credenciais: variável GOOGLE_CREDENTIALS (JSON) ou arquivo credenciais.json.
Código de saída ≠ 0 quando a Conab falhou → o GitHub Actions avisa por e-mail,
mesmo que o basis tenha sido recalculado com o histórico salvo.
"""
import logging
import sys

import pipeline_milho as pm


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s | %(message)s")
    log = logging.getLogger("agrobasis.milho.job")

    res = pm.atualizar_base_milho(pm.conectar_google())

    ult = res.ultima_semana_conab.strftime("%d/%m/%Y") if res.ultima_semana_conab is not None else "—"
    log.info("Conab: %s | semanas no histórico: %d | última semana: %s | linhas de basis: %d",
             "OK" if res.conab_ok else f"FALHA ({res.erro_conab})",
             res.semanas_conab_historico, ult, res.linhas_basis)
    log.info("A aba Planilha1 não foi alterada.")
    return 0 if res.conab_ok else 2


if __name__ == "__main__":
    sys.exit(main())
