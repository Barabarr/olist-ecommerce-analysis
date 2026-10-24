"""Carga e inspeção das tabelas brutas."""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from . import config
from .logger import get_logger

log = get_logger(__name__)


def carregar_dados(raw_path: Path) -> dict[str, pd.DataFrame]:
    """Carrega todas as tabelas brutas em um dicionário {nome: DataFrame}.

    Prefixos de CEP são lidos como texto e completados com zeros à esquerda
    (5 dígitos): como número, '01310' vira 1310."""
    dfs = {}
    log.info("Carregando dados de %s", raw_path)

    for nome, arquivo in config.ARQUIVOS.items():
        path = raw_path / arquivo
        col_cep = config.COLUNAS_CEP.get(nome)
        df = pd.read_csv(path, dtype={col_cep: str} if col_cep else None)
        if col_cep:
            df[col_cep] = df[col_cep].str.strip().str.zfill(5)
        dfs[nome] = df
        log.info("%-8s -> %8s linhas | %2d colunas", nome, f"{df.shape[0]:,}", df.shape[1])

    return dfs


def inspecionar_dados(dfs: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Gera um relatório estruturado por tabela: shape, nulos, duplicatas de
    linha inteira e duplicatas de CHAVE.

    As duplicatas de chave são as que importam para o grão: nenhuma linha de
    reviews é idêntica a outra, mas centenas de pedidos têm mais de uma
    avaliação -- e é isso que pode duplicar pedidos num join."""
    relatorio = []
    log.info("Inspeção dos dados")

    col_analiticas = {
        "items": ["price", "freight_value"],
        "payments": ["payment_value", "payment_installments"],
        "products": ["product_weight_g", "product_length_cm", "product_height_cm", "product_width_cm"],
    }

    for nome, df in dfs.items():
        linhas, colunas = df.shape
        duplicados = int(df.duplicated().sum())
        chave = config.CHAVES.get(nome)
        dup_chave = int(df.duplicated(subset=chave).sum()) if chave else None
        nulos = df.isnull().sum()
        nulos = nulos[nulos > 0]
        total_nulos = len(nulos)
        pct_nulos_max = round((nulos / linhas * 100).max(), 2) if total_nulos > 0 else 0.0

        log.info("Tabela %s: %s x %s | duplicados=%s | duplicados de chave %s=%s | colunas com nulos=%s",
                  nome.upper(), f"{linhas:,}", colunas, f"{duplicados:,}", chave,
                  "n/a" if dup_chave is None else f"{dup_chave:,}", total_nulos)

        colunas_analise = col_analiticas.get(nome, [])
        if colunas_analise:
            resumo = df[colunas_analise].describe().T[["mean", "std", "min", "max"]].round(2)
            log.info("Resumo estatístico (%s):\n%s", nome, resumo.to_string())

        relatorio.append({
            "tabela": nome,
            "linhas": linhas,
            "colunas": colunas,
            "col_com_nulos": total_nulos,
            "pct_nulos_max": pct_nulos_max,
            "duplicados": duplicados,
            "chave": "+".join(chave) if chave else None,
            "duplicados_chave": dup_chave,
        })

    return pd.DataFrame(relatorio)


def inspecionar_integridade(dfs: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Integridade referencial entre as tabelas brutas (config.INTEGRIDADE):
    quantos valores distintos da coluna filha não existem na tabela pai.

    Não corrige nada -- só mede. É a resposta, antes de qualquer join, para
    perguntas como "todo item tem pedido?" e "todo pedido tem item?"."""
    linhas = []
    for filha, col, pai, col_pai in config.INTEGRIDADE:
        valores = dfs[filha][col].dropna().unique()
        sem_par = int((~pd.Series(valores).isin(dfs[pai][col_pai])).sum())
        linhas.append({
            "filha": filha, "coluna": col, "pai": pai, "coluna_pai": col_pai,
            "valores_distintos": len(valores),
            "sem_correspondencia": sem_par,
            "pct_sem_correspondencia": round(sem_par / len(valores) * 100, 2) if len(valores) else 0.0,
        })
        if sem_par:
            log.info("integridade %s.%s -> %s.%s: %s de %s sem correspondência",
                      filha, col, pai, col_pai, f"{sem_par:,}", f"{len(valores):,}")
    return pd.DataFrame(linhas)
