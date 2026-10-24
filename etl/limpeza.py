"""Limpeza e padronização por tabela."""
from __future__ import annotations

import pandas as pd

from . import config
from .logger import get_logger

log = get_logger(__name__)


def tratar_orders(df: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """Limpa orders e SINALIZA os pedidos que não geram
    receita.

    Decisão de negócio (config.STATUS_SEM_RECEITA): todos os pedidos ficam no
    fato -- cancelamento também é objeto de análise -- e
    `pedido_valido_receita` diz quais entram nas métricas financeiras.

    Retorna também um dict de estatísticas para o manifesto."""
    orders = df.copy()

    cols_data = [
        "order_purchase_timestamp",
        "order_approved_at",
        "order_delivered_carrier_date",
        "order_delivered_customer_date",
        "order_estimated_delivery_date",
    ]
    orders[cols_data] = orders[cols_data].apply(pd.to_datetime, errors="coerce")

    desconhecidos = set(orders["order_status"].dropna().unique()) - config.STATUS_CONHECIDOS
    if desconhecidos:
        raise ValueError(
            f"Status de pedido não previstos em config.STATUS_CONHECIDOS: {sorted(desconhecidos)}"
        )

    orders["pedido_valido_receita"] = (
        ~orders["order_status"].isin(config.STATUS_SEM_RECEITA)
    ).astype(int)
    orders["status_exibicao"] = orders["order_status"].map(config.STATUS_PT)

    orders["pedido_aprovado"] = orders["order_approved_at"].notna().astype(int)
    orders["pedido_entregue"] = orders["order_delivered_customer_date"].notna().astype(int)

    stats = {
        "linhas": len(orders),
        "pedidos_por_status": orders["order_status"].value_counts().to_dict(),
        "pedidos_sem_receita": int((orders["pedido_valido_receita"] == 0).sum()),
    }

    log.info("orders -> %s pedidos mantidos | %s sinalizados sem receita (%s)",
              f"{len(orders):,}", f"{stats['pedidos_sem_receita']:,}",
              ", ".join(sorted(config.STATUS_SEM_RECEITA)))

    for col in ["order_approved_at", "order_delivered_carrier_date", "order_delivered_customer_date"]:
        n = orders[col].isna().sum()
        log.info("%s: %s (%.1f%%)", col, f"{n:,}", n / len(orders) * 100)

    return orders, stats


def tratar_customers(df: pd.DataFrame) -> pd.DataFrame:
    """Padroniza customers e mantém `customer_unique_id`, necessário para medir
    recompra/recorrência mais adiante."""
    customers = df.copy()

    customers["customer_state"] = customers["customer_state"].str.upper().str.strip()
    customers["customer_city"] = customers["customer_city"].str.strip().str.title()
    customers["customer_state"] = customers["customer_state"].astype("category")

    log.info("customers -> %s registros padronizados", f"{len(customers):,}")

    return customers[[
        "customer_id",
        "customer_unique_id",
        "customer_zip_code_prefix",
        "customer_city",
        "customer_state",
    ]]


def tratar_sellers(df: pd.DataFrame) -> pd.DataFrame:
    """Padroniza a UF do vendedor"""
    sellers = df.copy()
    sellers["seller_state"] = sellers["seller_state"].str.upper().str.strip().astype("category")

    log.info("sellers -> %s registros padronizados", f"{len(sellers):,}")

    return sellers[["seller_id", "seller_city", "seller_state", "seller_zip_code_prefix"]]


def tratar_items(df: pd.DataFrame) -> pd.DataFrame:
    items = df.copy()
    antes = len(items)

    for col in ["price", "freight_value"]:
        items[col] = pd.to_numeric(items[col], errors="coerce")

    items = items.dropna(subset=["price", "freight_value"])

    log.info("items -> %s removidos | %s mantidos", f"{antes - len(items):,}", f"{len(items):,}")
    return items


def tratar_geo(df: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """Reduz a geolocalização a 1 linha por prefixo de CEP.

    Ordem importa: PRIMEIRO descarta os pontos fora do bounding box do Brasil
    (erro de geocodificação conhecido nesse dataset), DEPOIS agrega.

    A coordenada do CEP é a MEDIANA dos pontos (em média ~29 por prefixo):
    resiste a pontos isolados longe dos demais. `n_pontos` registra quantos pontos sustentam
    cada coordenada."""
    geo = df.copy()
    antes = len(geo)

    dentro_bbox = (
        geo["geolocation_lat"].between(config.BRASIL_LAT_MIN, config.BRASIL_LAT_MAX)
        & geo["geolocation_lng"].between(config.BRASIL_LON_MIN, config.BRASIL_LON_MAX)
    )
    fora_bbox = int((~dentro_bbox).sum())

    geo = (
        geo[dentro_bbox]
        .groupby("geolocation_zip_code_prefix", as_index=False)
        .agg(
            geolocation_lat=("geolocation_lat", "median"),
            geolocation_lng=("geolocation_lng", "median"),
            geolocation_city=("geolocation_city", "first"),
            geolocation_state=("geolocation_state", "first"),
            n_pontos=("geolocation_lat", "size"),
        )
    )

    stats = {
        "linhas_antes": antes,
        "pontos_fora_do_brasil_removidos": fora_bbox,
        "ceps_unicos": len(geo),
    }

    log.info(
        "geo -> %s pontos fora do Brasil removidos | %s CEPs únicos (coordenada = mediana dos pontos)",
        f"{fora_bbox:,}", f"{len(geo):,}",
    )
    return geo, stats


def tratar_products(df: pd.DataFrame) -> pd.DataFrame:
    """Limpa products. A coluna `category_en` (tradução em inglês) foi removida
    do fluxo --  `categoria_exibicao`: o nome em português,
    formatado(sem underscore, com
    Maiúsculas), a coluna original vem em snake_case.

    Campos da ficha do produto (tamanho do nome/descrição, nº de fotos) sem
    informação ficam NULOS e sinalizados em `ficha_incompleta` -- preencher
    com 0 faria um produto sem cadastro parecer um produto "sem fotos".
    Dimensões físicas ausentes são imputadas pela mediana da categoria e
    sinalizadas em `dimensoes_imputadas`."""
    products = df.copy()

    products["product_category_name"] = products["product_category_name"].fillna("sem_categoria")

    cols_ficha = ["product_name_lenght", "product_description_lenght", "product_photos_qty"]
    cols_ficha = [c for c in cols_ficha if c in products.columns]
    products["ficha_incompleta"] = products[cols_ficha].isna().any(axis=1).astype(int)

    cols_dim = ["product_weight_g", "product_length_cm", "product_height_cm", "product_width_cm"]
    products["dimensoes_imputadas"] = products[cols_dim].isna().any(axis=1).astype(int)
    for col in cols_dim:
        mediana_cat = products.groupby("product_category_name")[col].transform("median")
        mediana_global = products[col].median()
        products[col] = products[col].fillna(mediana_cat).fillna(mediana_global)

    products["categoria_exibicao"] = (
        products["product_category_name"]
        .str.replace("_", " ", regex=False)
        .str.title()
    )

    products["product_category_name"] = products["product_category_name"].astype("category")
    products["categoria_exibicao"] = products["categoria_exibicao"].astype("category")

    log.info("products -> %s com ficha incompleta (mantida nula) | %s com dimensões imputadas",
              f"{products['ficha_incompleta'].sum():,}", f"{products['dimensoes_imputadas'].sum():,}")

    return products


def tratar_reviews(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """Escolhe UMA avaliação por pedido e deriva dela as duas saídas.

    Regra: a avaliação mais recente por `review_creation_date`; empates
    (mesma data de criação) são desfeitos por `review_answer_timestamp` e,
    por fim, `review_id` -- assim o resultado não depende da ordem do CSV
    nem do algoritmo de ordenação.

    `reviews_nlp` sai da MESMA avaliação escolhida (só as que têm texto).
    

    `review_id` é mantido: o mesmo texto pode estar ligado a mais de um
    pedido, e contar avaliações distintas exige o id."""
    reviews = df.copy()

    reviews["review_creation_date"] = pd.to_datetime(reviews["review_creation_date"], errors="coerce")
    reviews["review_answer_timestamp"] = pd.to_datetime(reviews["review_answer_timestamp"], errors="coerce")

    reviews["possui_comentario"] = reviews["review_comment_message"].notna().astype(int)
    reviews["review_positivo"] = (reviews["review_score"] >= 4).astype(int)
    reviews["review_negativo"] = (reviews["review_score"] <= 2).astype(int)

    escolhidas = (
        reviews.dropna(subset=["review_score"])
        .sort_values(
            ["order_id", "review_creation_date", "review_answer_timestamp", "review_id"],
            kind="stable", na_position="first",   # data nula nunca "vence" uma data válida
        )
        .drop_duplicates(subset="order_id", keep="last")
        .reset_index(drop=True)
    )
    escolhidas["review_score"] = escolhidas["review_score"].astype(int)

    reviews_analitico = escolhidas[
        ["order_id", "review_id", "review_score", "possui_comentario", "review_positivo", "review_negativo"]
    ]

    reviews_nlp = (
        escolhidas.loc[
            escolhidas["possui_comentario"] == 1,
            ["order_id", "review_id", "review_score", "review_comment_title", "review_comment_message",
             "review_positivo", "review_negativo"],
        ]
        .reset_index(drop=True)
    )

    stats = {
        "linhas_antes": len(reviews),
        "pedidos_com_mais_de_uma_avaliacao": int((reviews["order_id"].value_counts() > 1).sum()),
        "avaliacoes_descartadas_por_pedido": len(reviews) - len(reviews_analitico),
        "review_ids_ligados_a_mais_de_um_pedido": int(
            (reviews.groupby("review_id")["order_id"].nunique() > 1).sum()
        ),
        "pedidos_com_texto": len(reviews_nlp),
    }

    log.info("reviews -> analítico: %s | NLP: %s com texto | %s avaliações extras descartadas",
              f"{len(reviews_analitico):,}", f"{len(reviews_nlp):,}",
              f"{stats['avaliacoes_descartadas_por_pedido']:,}")
    return reviews_analitico, reviews_nlp, stats


def tratar_payments(df: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    payments = df.copy()
    antes = len(payments)

    payments = payments[
        (payments["payment_value"] >= 0) & (payments["payment_type"] != "not_defined")
    ].copy()
    payments["payment_type"] = payments["payment_type"].str.lower().str.strip()
    payments["pagamento_cartao"] = (payments["payment_type"] == "credit_card").astype(int)
    payments["pagamento_parcelado"] = (payments["payment_installments"] > 1).astype(int)

    # rótulo pronto para gráfico;
    # 'Outro' como fallback defensivo caso apareça um payment_type não mapeado
    payments["forma_pagamento_exibicao"] = (
        payments["payment_type"].map(config.FORMAS_PAGAMENTO_PT).fillna("Outro")
    )

    qtd_formas = (
        payments.groupby("order_id")["payment_type"].nunique()
        .reset_index().rename(columns={"payment_type": "qtd_formas_pagamento"})
    )
    payments = payments.merge(qtd_formas, on="order_id", how="left", validate="many_to_one")
    payments["payment_type"] = payments["payment_type"].astype("category")

    stats = {"linhas_antes": antes, "removidos_valor_negativo_ou_not_defined": antes - len(payments)}

    log.info("payments -> %s registros | %s removidos (valor negativo ou 'not_defined')",
              f"{len(payments):,}", f"{stats['removidos_valor_negativo_ou_not_defined']:,}")
    return payments, stats
