"""Construção do modelo dimensional final (star schema).

Modelo implementado:

    dim_data                     dim_cliente
         |                            |
         |                     dim_geo_cliente
         |                            |
         +------- dataset_analitico --+   <- fato, grão = PEDIDO
                        |
                   fato_itens              <- fato, grão = ITEM
                   /          \\
            dim_produtos   dim_sellers
                                 |
                          dim_geo_vendedor

Por que separar em fato + dimensão em vez do dataset único "largo" que
tínhamos antes: evita repetir o mesmo atributo (nome do dia da semana,
cidade do cliente, categoria do produto) em milhares de linhas de fato --
cada atributo descritivo mora uma vez só na dimensão, e o fato guarda
apenas a chave (FK) para ele. Isso é exatamente o que o Power BI espera
para montar um modelo em estrela eficiente.
"""
from __future__ import annotations

import re
import unicodedata

import numpy as np
import pandas as pd

from . import config
from .logger import get_logger

log = get_logger(__name__)


def construir_dim_data(orders: pd.DataFrame) -> pd.DataFrame:
    """Dimensão de calendário: 1 linha por dia, em ANOS COMPLETOS -- de 1º de
    janeiro do ano da primeira compra a 31 de dezembro do ano da última data
    registrada em qualquer coluna de data dos pedidos (inclui entrega e prazo
    prometido, que vão além da última compra).
    """
    cols_data = [
        "order_purchase_timestamp", "order_approved_at", "order_delivered_carrier_date",
        "order_delivered_customer_date", "order_estimated_delivery_date",
    ]
    primeiro_ano = orders["order_purchase_timestamp"].min().year
    ultimo_ano = orders[cols_data].max().max().year

    dim = pd.DataFrame({"data": pd.date_range(f"{primeiro_ano}-01-01", f"{ultimo_ano}-12-31", freq="D")})
    dim["data_id"] = dim["data"].dt.strftime("%Y%m%d").astype(int)  # chave técnica p/ join
    dim["ano"] = dim["data"].dt.year
    dim["mes"] = dim["data"].dt.month
    dim["mes_nome"] = dim["mes"].map(config.MESES_PT)            # ordenar por `mes` no Power BI
    dim["ano_mes"] = dim["data"].dt.strftime("%Y-%m")            # texto que já ordena certo
    dim["dia"] = dim["data"].dt.day
    dim["trimestre"] = dim["data"].dt.quarter

    dia_semana_en = dim["data"].dt.day_name()
    dim["dia_semana_num"] = dia_semana_en.map(lambda d: config.DIAS_SEMANA_PT[d][0])
    dim["dia_semana"] = dia_semana_en.map(lambda d: config.DIAS_SEMANA_PT[d][1])
    dim["dia_semana"] = pd.Categorical(
        dim["dia_semana"],
        categories=[nome for _, nome in sorted(config.DIAS_SEMANA_PT.values())],
        ordered=True,
    )
    dim["final_semana"] = dia_semana_en.isin(["Saturday", "Sunday"]).astype(int)

    dim = dim[["data_id", "data", "ano", "mes", "mes_nome", "ano_mes", "dia", "trimestre",
               "dia_semana_num", "dia_semana", "final_semana"]]

    log.info("dim_data -> %s datas (%s a %s)", f"{len(dim):,}",
              dim["data"].min().date(), dim["data"].max().date())
    return dim


def construir_dim_geo(geo: pd.DataFrame, zips_usados: pd.Series, papel: str) -> pd.DataFrame:
    """Recorta a geolocalização tratada apenas para os CEPs efetivamente
    usados por clientes ou por vendedores -- gera `dim_geo_cliente` e
    `dim_geo_vendedor` a partir da mesma tabela-fonte, cada uma só com o
    que é relevante para aquele papel."""
    zips = set(zips_usados.dropna().unique())
    dim = geo[geo["geolocation_zip_code_prefix"].isin(zips)].reset_index(drop=True)

    log.info("dim_geo_%s -> %s CEPs (de %s usados nos dados)",
              papel, f"{len(dim):,}", f"{len(zips):,}")
    return dim


def construir_fato_pagamentos(payments: pd.DataFrame) -> pd.DataFrame:
    """Fato de pagamentos -- grão = 1 linha por forma de pagamento usada em
    cada pedido (`order_id` + `payment_sequential`). Existe porque um pedido
    pode ter mais de uma forma de pagamento, então
    não dá para resumir isso numa única coluna categórica em
    `dataset_analitico` sem perder informação -- daí a tabela separada,
    ligada por `order_id`.

    `pagamento_parcelado` e `forma_pagamento_exibicao` NÃO são recalculados
    aqui -- já vêm prontos de `tratar_payments`."""
    fato = payments[[
        "order_id",
        "payment_sequential",
        "payment_type",
        "forma_pagamento_exibicao",
        "payment_installments",
        "payment_value",
        "pagamento_parcelado",
        "pagamento_cartao",
    ]].copy()

    fato = (
        fato
        .sort_values(["order_id", "payment_sequential"])
        .reset_index(drop=True)
    )

    log.info("fato_pagamentos -> %s registros (%s pedidos)",
              f"{len(fato):,}", f"{fato['order_id'].nunique():,}")

    return fato


def _haversine_km(lat1: pd.Series, lon1: pd.Series, lat2: pd.Series, lon2: pd.Series) -> pd.Series:
    """Distância em linha reta (km) entre dois pontos lat/lon, fórmula de
    Haversine (aproxima a Terra como esfera -- erro típico < 0.5%, mais que
    suficiente para uma métrica de "frete x distância", que não precisa de
    precisão de rota real)."""
    lat1, lon1, lat2, lon2 = map(np.radians, [lat1, lon1, lat2, lon2])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = np.sin(dlat / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin(dlon / 2) ** 2
    return 2 * 6371 * np.arcsin(np.sqrt(a))  # 6371 km = raio médio da Terra


def adicionar_distancia_km(
    fato_itens: pd.DataFrame,
    orders: pd.DataFrame,
    customers: pd.DataFrame,
    sellers: pd.DataFrame,
    geo: pd.DataFrame,
) -> pd.DataFrame:
    """adiciona `distancia_km` a `fato_itens` -- distância
    aproximada entre o CEP do cliente (comprador) e o CEP do vendedor de
    cada item, calculada via Haversine sobre a coordenada representativa de
    cada prefixo de CEP em `geo` (mediana dos pontos, ver `tratar_geo`).
    É distância em LINHA RETA entre centróides de prefixo de 5 dígitos --
    não é a rota percorrida pelo transporte.

    Fica nula quando o CEP do cliente ou do vendedor não tem geolocalização
    correspondente na tabela de origem -- não é erro, é limite conhecido do
    dataset (nem todo CEP do Brasil está mapeado na base pública da Olist)."""
    geo_lookup = geo.drop_duplicates(subset="geolocation_zip_code_prefix").set_index(
        "geolocation_zip_code_prefix"
    )[["geolocation_lat", "geolocation_lng"]]

    pedido_cliente = orders[["order_id", "customer_id"]].merge(
        customers[["customer_id", "customer_zip_code_prefix"]], on="customer_id", how="left",
        validate="many_to_one",
    )

    base = (
        fato_itens[["order_id", "seller_id"]]
        .merge(pedido_cliente[["order_id", "customer_zip_code_prefix"]], on="order_id", how="left",
               validate="many_to_one")
        .merge(sellers[["seller_id", "seller_zip_code_prefix"]], on="seller_id", how="left",
               validate="many_to_one")
    )

    base = base.merge(
        geo_lookup, left_on="customer_zip_code_prefix", right_index=True, how="left",
        validate="many_to_one",
    ).rename(columns={"geolocation_lat": "cliente_lat", "geolocation_lng": "cliente_lng"})

    base = base.merge(
        geo_lookup, left_on="seller_zip_code_prefix", right_index=True, how="left",
        validate="many_to_one",
    ).rename(columns={"geolocation_lat": "vendedor_lat", "geolocation_lng": "vendedor_lng"})

    fato_itens = fato_itens.copy()
    fato_itens["distancia_km"] = _haversine_km(
        base["cliente_lat"], base["cliente_lng"], base["vendedor_lat"], base["vendedor_lng"]
    ).round(1).values

    pct_nulo = fato_itens["distancia_km"].isna().mean() * 100
    log.info("distancia_km -> calculada para %s itens (%.1f%% sem CEP mapeado)",
              f"{fato_itens['distancia_km'].notna().sum():,}", pct_nulo)

    return fato_itens


def _sem_acento(texto: str) -> str:
    """'não' -> 'nao'. Usado como chave de comparação, não para exibição."""
    return unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode("ascii")


_STOPWORDS_SEM_ACENTO = frozenset(_sem_acento(w) for w in config.STOPWORDS_PT)


def _tokenizar_pt(texto) -> list[str]:
    """Tokeniza um comentário: minúsculas, remove pontuação/números,
    descarta stopwords em português e palavras com menos de 3 letras.

    Devolve as palavras na forma escrita (com acento, se houver); a
    comparação com as stopwords é feita sem acento, porque clientes escrevem
    "esta"/"está" e "nao"/"não" indistintamente."""
    if not isinstance(texto, str):
        return []
    texto = texto.lower()
    texto = re.sub(r"[^a-zà-ÿ\s]", " ", texto)
    return [t for t in texto.split() if len(t) >= 3 and _sem_acento(t) not in _STOPWORDS_SEM_ACENTO]


def construir_fato_palavras_review(reviews_nlp: pd.DataFrame) -> pd.DataFrame:
    """explode cada comentário em 1 linha por OCORRÊNCIA de
    palavra (uma palavra repetida no mesmo comentário gera várias linhas),
    já tokenizado e sem stopwords em português.

    Variantes com e sem acento são unificadas: `palavra_normalizada` ('nao')
    é a chave e `palavra` é a grafia mais frequente dessa chave ('não'),
    pronta para exibir. Sem isso, "não" e "nao" disputavam o ranking como
    palavras diferentes.

    `review_id` permite contar AVALIAÇÕES distintas que mencionam a palavra
    (o mesmo texto pode estar ligado a mais de um pedido).

    `review_positivo`/`review_negativo` vêm junto (denormalizados), então
    filtrar por sentimento não depende de nenhum relacionamento -- mesmo
    padrão usado em `categoria_exibicao` dentro de `dataset_analitico`."""
    tmp = reviews_nlp[["order_id", "review_id", "review_comment_message",
                       "review_positivo", "review_negativo"]].copy()
    tmp["forma"] = tmp["review_comment_message"].apply(_tokenizar_pt)
    tmp = tmp.explode("forma").dropna(subset=["forma"])
    tmp["palavra_normalizada"] = tmp["forma"].map(_sem_acento)

    grafia_mais_comum = (
        tmp.groupby(["palavra_normalizada", "forma"]).size().reset_index(name="n")
        .sort_values(["palavra_normalizada", "n", "forma"], ascending=[True, False, True], kind="stable")
        .drop_duplicates(subset="palavra_normalizada", keep="first")
        .rename(columns={"forma": "palavra"})[["palavra_normalizada", "palavra"]]
    )
    tmp = tmp.merge(grafia_mais_comum, on="palavra_normalizada", how="left", validate="many_to_one")

    tmp = tmp[["order_id", "review_id", "palavra", "palavra_normalizada",
               "review_positivo", "review_negativo"]].reset_index(drop=True)

    log.info("fato_palavras_review -> %s ocorrências | %s palavras distintas (de %s reviews com texto)",
              f"{len(tmp):,}", f"{tmp['palavra_normalizada'].nunique():,}",
              f"{reviews_nlp['order_id'].nunique():,}")

    return tmp
