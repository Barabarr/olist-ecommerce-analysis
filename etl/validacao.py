"""Contrato de qualidade de dados das tabelas exportadas para o Power BI.

A validação só VERIFICA -- não remove linhas nem preenche valores. Toda
transformação acontece antes (limpeza / features / modelagem), para que a
resposta a "quantas linhas saíram e por quê?" esteja no manifesto, e não
escondida dentro de um passo de validação.

Três camadas, todas checadas ANTES da exportação:
  1. Schema por tabela (Pandera): tipos, faixas, nulos, unicidade da chave.
  2. Integridade referencial: toda chave estrangeira existe na tabela pai.
  3. Reconciliação entre fatos: totais de itens e de pagamentos batem com
     o fato de pedidos (mesmo universo de pedidos em todas as tabelas).

As falhas são coletadas e reportadas juntas (em vez de parar na primeira);
qualquer falha interrompe o pipeline.

Pandera é dependência obrigatória (requirements.txt): um motor só, um
comportamento só, em qualquer ambiente.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import config
from .logger import get_logger

log = get_logger(__name__)

try:
    import pandera
    import pandera.pandas as pa
    from pandera.pandas import Check, Column, DataFrameSchema
except ImportError as exc:  # pragma: no cover - depende do ambiente
    raise ImportError(
        "O contrato de validação usa Pandera. Instale as dependências com "
        "`pip install -r requirements.txt`."
    ) from exc



_FLAG = Check.isin([0, 1])
_NAO_NEG = Check.ge(0)


def _flag(nullable: bool = False) -> Column:
    return Column(None, _FLAG, nullable=nullable)


def _etapas_somam_ao_total(df: pd.DataFrame) -> pd.Series:
    etapas = df[["dias_aprovacao", "dias_preparacao", "dias_transporte"]].sum(axis=1, min_count=3)
    completos = etapas.notna() & df["dias_entrega"].notna()
    return ~completos | ((etapas - df["dias_entrega"]).abs() < 1e-6)


def _recorrencia_so_em_pedidos_validos(df: pd.DataFrame) -> pd.Series:
    return df["numero_pedido_cliente"].isna() == (df["pedido_valido_receita"] == 0)


def _dim_data_continua(df: pd.DataFrame) -> bool:
    return bool(df["data"].sort_values().diff().dropna().eq(pd.Timedelta(days=1)).all())


def _schemas() -> dict[str, DataFrameSchema]:
    lat = Check.in_range(config.BRASIL_LAT_MIN, config.BRASIL_LAT_MAX)
    lng = Check.in_range(config.BRASIL_LON_MIN, config.BRASIL_LON_MAX)
    geo = DataFrameSchema(
        {
            "geolocation_zip_code_prefix": Column(None, Check.str_length(5, 5), unique=True),
            "geolocation_lat": Column(float, lat),
            "geolocation_lng": Column(float, lng),
        },
        strict=False,
    )
    return {
        "dataset_analitico": DataFrameSchema(
            {
                "order_id": Column(None, unique=True),
                "customer_id": Column(None, unique=True),   # no Olist, 1 customer_id por pedido
                "order_status": Column(None, Check.isin(sorted(config.STATUS_CONHECIDOS))),
                "status_exibicao": Column(None),
                "pedido_valido_receita": _flag(),
                "data_id": Column(None),
                "pedido_aprovado": _flag(),
                "pedido_entregue": _flag(),
                "dias_aprovacao": Column(float, _NAO_NEG, nullable=True),
                "dias_preparacao": Column(float, _NAO_NEG, nullable=True),
                "dias_transporte": Column(float, _NAO_NEG, nullable=True),
                "dias_entrega": Column(float, _NAO_NEG, nullable=True),
                "dias_atraso": Column(float, nullable=True),
                "pedido_atrasado": Column("Int64", _FLAG, nullable=True),
                "flag_datas_inconsistentes": _flag(),
                "valor_produtos": Column(float, _NAO_NEG),
                "valor_frete": Column(float, _NAO_NEG),
                "qtd_itens": Column(None, _NAO_NEG),
                "pedido_sem_itens": _flag(),
                "pct_frete": Column(float, _NAO_NEG, nullable=True),
                "review_score": Column(float, Check.in_range(1, 5), nullable=True),
                "review_positivo": _flag(nullable=True),
                "review_negativo": _flag(nullable=True),
                "review_comentario": _flag(nullable=True),
                "valor_pago": Column(float, _NAO_NEG, nullable=True),
                "numero_pedido_cliente": Column("Int64", Check.ge(1), nullable=True),
                "cliente_recorrente": Column("Int64", _FLAG, nullable=True),
            },
            checks=[
                Check(_etapas_somam_ao_total,
                      error="aprovação + preparação + transporte deve ser igual a dias_entrega"),
                Check(_recorrencia_so_em_pedidos_validos,
                      error="numero_pedido_cliente deve existir só (e sempre) em pedidos válidos"),
            ],
            strict=False,
        ),
        "fato_itens": DataFrameSchema(
            {
                "order_id": Column(None),
                "order_item_id": Column(None, Check.ge(1)),
                "product_id": Column(None),
                "seller_id": Column(None),
                "price": Column(float, Check.gt(0)),
                "freight_value": Column(float, _NAO_NEG),
                "distancia_km": Column(float, _NAO_NEG, nullable=True),
            },
            unique=["order_id", "order_item_id"],
            strict=False,
        ),
        "fato_pagamentos": DataFrameSchema(
            {
                "order_id": Column(None),
                "payment_sequential": Column(None, Check.ge(1)),
                "payment_installments": Column(None, _NAO_NEG),
                "payment_value": Column(float, _NAO_NEG),
            },
            unique=["order_id", "payment_sequential"],
            strict=False,
        ),
        "reviews_nlp": DataFrameSchema(
            {
                "order_id": Column(None, unique=True),
                "review_id": Column(None),
                "review_score": Column(None, Check.in_range(1, 5)),
                "review_comment_message": Column(None),
            },
            strict=False,
        ),
        "fato_palavras_review": DataFrameSchema(
            {
                "order_id": Column(None),
                "review_id": Column(None),
                "palavra": Column(None),
                "palavra_normalizada": Column(None),
            },
            strict=False,
        ),
        "dim_data": DataFrameSchema(
            {"data_id": Column(None, unique=True), "data": Column(None, unique=True)},
            checks=[Check(_dim_data_continua, error="dim_data precisa ser contínua (sem dias faltando)")],
            strict=False,
        ),
        "dim_cliente": DataFrameSchema(
            {"customer_id": Column(None, unique=True), "customer_unique_id": Column(None)},
            strict=False,
        ),
        "dim_produtos": DataFrameSchema({"product_id": Column(None, unique=True)}, strict=False),
        "dim_sellers": DataFrameSchema({"seller_id": Column(None, unique=True)}, strict=False),
        "dim_geo_cliente": geo,
        "dim_geo_vendedor": geo,
    }


# (filha, coluna, pai, coluna_pai): todo valor da filha precisa existir no pai.
_INTEGRIDADE = [
    ("fato_itens", "order_id", "dataset_analitico", "order_id"),
    ("fato_pagamentos", "order_id", "dataset_analitico", "order_id"),
    ("reviews_nlp", "order_id", "dataset_analitico", "order_id"),
    ("fato_palavras_review", "order_id", "dataset_analitico", "order_id"),
    ("dataset_analitico", "data_id", "dim_data", "data_id"),
    ("dataset_analitico", "customer_id", "dim_cliente", "customer_id"),
    ("fato_itens", "product_id", "dim_produtos", "product_id"),
    ("fato_itens", "seller_id", "dim_sellers", "seller_id"),
    # UF no mesmo formato nas duas pontas (comparação vendedor x cliente)
    ("dim_sellers", "seller_state", "dim_cliente", "customer_state"),
]

# (descrição, tabela_a, coluna_a, tabela_b, coluna_b): somas que precisam bater.
_RECONCILIACAO = [
    ("receita de produtos", "fato_itens", "price", "dataset_analitico", "valor_produtos"),
    ("frete", "fato_itens", "freight_value", "dataset_analitico", "valor_frete"),
    ("valor pago", "fato_pagamentos", "payment_value", "dataset_analitico", "valor_pago"),
]


def validar_modelo(tabelas: dict[str, pd.DataFrame]) -> dict:
    """Aplica o contrato inteiro (schemas, integridade, reconciliação) às
    tabelas que serão exportadas. Levanta ValueError se qualquer regra
    falhar; devolve um resumo para o manifesto se tudo passar."""
    falhas: list[str] = []

    log.info("Validação: schemas por tabela (pandera %s)", pandera.__version__)
    for nome, schema in _schemas().items():
        try:
            schema.validate(tabelas[nome], lazy=True)
        except pa.errors.SchemaErrors as exc:
            casos = exc.failure_cases
            log.error("Falhas de schema em %s:\n%s", nome, casos.head(20).to_string(index=False))
            falhas.append(f"{nome}: {len(casos)} caso(s) fora do contrato")

    log.info("Validação: integridade referencial")
    for filha, col, pai, col_pai in _INTEGRIDADE:
        valores = pd.Series(tabelas[filha][col].dropna().unique())
        orfaos = int((~valores.isin(tabelas[pai][col_pai])).sum())
        if orfaos:
            falhas.append(f"{filha}.{col}: {orfaos:,} valor(es) sem correspondência em {pai}.{col_pai}")

    log.info("Validação: reconciliação entre fatos")
    reconciliacao = {}
    for descricao, ta, ca, tb, cb in _RECONCILIACAO:
        soma_a, soma_b = float(tabelas[ta][ca].sum()), float(tabelas[tb][cb].sum())
        reconciliacao[descricao] = {ta: round(soma_a, 2), tb: round(soma_b, 2)}
        if not np.isclose(soma_a, soma_b, rtol=0, atol=0.01):
            falhas.append(f"{descricao}: {ta}.{ca} = {soma_a:,.2f} | {tb}.{cb} = {soma_b:,.2f}")

    itens_fato = len(tabelas["fato_itens"])
    itens_pedidos = int(tabelas["dataset_analitico"]["qtd_itens"].sum())
    if itens_fato != itens_pedidos:
        falhas.append(f"qtd de itens: fato_itens = {itens_fato:,} | soma de qtd_itens = {itens_pedidos:,}")

    if falhas:
        for f in falhas:
            log.error(" x %s", f)
        raise ValueError(f"Validação falhou: {len(falhas)} problema(s). Nada foi exportado.")

    log.info("Validação OK -> %s tabelas | %s checagens de integridade | %s reconciliações",
              len(_schemas()), len(_INTEGRIDADE), len(_RECONCILIACAO) + 1)
    return {
        "tabelas_validadas": sorted(_schemas()),
        "checagens_integridade": len(_INTEGRIDADE),
        "reconciliacao": reconciliacao,
    }
