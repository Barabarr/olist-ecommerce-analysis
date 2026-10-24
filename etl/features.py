"""Feature engineering, agregações e montagem do dataset final."""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import config
from .logger import get_logger

log = get_logger(__name__)

# Etapas do lead time: aprovação + preparação + transporte = entrega.
ETAPAS_LEAD_TIME = ["dias_aprovacao", "dias_preparacao", "dias_transporte"]


def _dias(fim: pd.Series, inicio: pd.Series) -> pd.Series:
    """Duração em dias fracionários (0.5 = 12 h). NaN se alguma ponta for nula.

    Usa total_seconds(): `.dt.days` devolveria só a parte inteira (uma
    aprovação de 23 h viraria 0 dia) e as etapas deixariam de somar ao total."""
    return (fim - inicio).dt.total_seconds() / 86_400


def feature_engineering(orders: pd.DataFrame, items: pd.DataFrame) -> pd.DataFrame:
    """Gera as métricas de negócio no grão de pedido. Atributos de calendário
    (ano, mês, dia da semana, fim de semana) NÃO ficam mais aqui -- moram em
    `dim_data`; o fato guarda só `data_id`, a chave para buscar isso lá.

    Pedidos com datas fora de sequência (ex.: despacho registrado antes da
    aprovação) são MANTIDOS e sinalizados em `flag_datas_inconsistentes`;
    só as durações por etapa ficam nulas, porque não dá para saber qual das
    datas está errada. Prazo total e atraso continuam válidos."""
    df = orders.copy()
    ts = df["order_purchase_timestamp"]

    df["data_id"] = ts.dt.strftime("%Y%m%d").astype(int)  # FK -> dim_data

    df["turno"] = pd.cut(
        ts.dt.hour,
        bins=[-1, 5, 11, 17, 20, 23],
        labels=["madrugada", "manha", "tarde", "noite", "fim_de_noite"],
    )

    df["dias_aprovacao"] = _dias(df["order_approved_at"], ts)
    df["dias_preparacao"] = _dias(df["order_delivered_carrier_date"], df["order_approved_at"])
    df["dias_transporte"] = _dias(df["order_delivered_customer_date"], df["order_delivered_carrier_date"])
    df["dias_entrega"] = _dias(df["order_delivered_customer_date"], ts)

    # Regra de atraso: dias de CALENDÁRIO após a data prometida. A data
    # estimada vem sempre às 00:00; normalize() zera a hora da entrega para
    # deixar a regra explícita (entregue no próprio dia prometido = 0).
    df["dias_atraso"] = (
        df["order_delivered_customer_date"].dt.normalize() - df["order_estimated_delivery_date"]
    ).dt.days
    
    LIMITES = [-np.inf, -15, -8, -1, 0, 3, 7, 14, np.inf]   # intervalos fechados à direita
    ROTULOS = ["15+ dias antes", "8 a 14 dias antes", "1 a 7 dias antes", "No dia prometido",
           "1 a 3 dias depois", "4 a 7 dias depois", "8 a 14 dias depois", "15+ dias depois"]
    faixa = pd.cut(df["dias_atraso"], bins=LIMITES, labels=ROTULOS)
    df["faixa_atraso"] = faixa.astype("string")
    df["faixa_atraso_ordem"] = (faixa.cat.codes + 1).where(faixa.notna()).astype("Int64")




    inconsistente = (df[ETAPAS_LEAD_TIME + ["dias_entrega"]] < 0).any(axis=1)
    df["flag_datas_inconsistentes"] = inconsistente.astype(int)
    df.loc[inconsistente, ETAPAS_LEAD_TIME] = np.nan
    df.loc[df["dias_entrega"] < 0, "dias_entrega"] = np.nan

    # 1/0 quando entregue; nulo quando ainda não entregue (Int64 aceita nulo
    # sem virar float 1.0/0.0).
    df["pedido_atrasado"] = (
        (df["dias_atraso"] > 0).astype("Int64").where(df["order_delivered_customer_date"].notna())
    )

    resumo_items = (
        items.groupby("order_id")
        .agg({"price": "sum", "freight_value": "sum", "order_item_id": "count"})
        .rename(columns={"price": "valor_produtos", "freight_value": "valor_frete", "order_item_id": "qtd_itens"})
        .reset_index()
    )
    df = df.merge(resumo_items, on="order_id", how="left", validate="one_to_one")

    # Pedido sem nenhum item (quase todos cancelados/indisponíveis): fica no
    # fato, sinalizado. Soma de itens inexistentes = 0.
    df["pedido_sem_itens"] = df["qtd_itens"].isna().astype(int)
    df[["valor_produtos", "valor_frete"]] = df[["valor_produtos", "valor_frete"]].fillna(0.0)
    df["qtd_itens"] = df["qtd_itens"].fillna(0).astype(int)

    # Indefinido (nulo) quando não há valor de produtos.
    df["pct_frete"] = np.where(
        df["valor_produtos"] > 0,
        (df["valor_frete"] / df["valor_produtos"]) * 100,
        np.nan,
    )

    log.info("feature engineering -> %s pedidos com datas inconsistentes (etapas anuladas) | %s pedidos sem itens",
              f"{df['flag_datas_inconsistentes'].sum():,}", f"{df['pedido_sem_itens'].sum():,}")
    return df


def calcular_recorrencia(orders_fe: pd.DataFrame, customers: pd.DataFrame) -> pd.DataFrame:
    """Métrica de cliente recorrente, usando `customer_unique_id` (a pessoa
    real, não o `customer_id` por pedido). Sem isso não dá para responder
    'quantos clientes voltaram a comprar?' -- pergunta central de qualquer
    análise de e-commerce.

    Regra (config.RECOMPRA_REGRA): só pedidos válidos para receita contam;
    com 'dia_diferente', pedidos do mesmo cliente no mesmo dia formam UMA
    ocasião de compra.

    Gera, por pedido:
      - numero_pedido_cliente: ordem da ocasião de compra do cliente
        (1 = primeira compra). Nulo para pedidos sem receita.
      - cliente_recorrente: 1 se o pedido é uma recompra; nulo para pedidos
        sem receita.
    """
    base = orders_fe[["order_id", "customer_id", "order_purchase_timestamp", "pedido_valido_receita"]].merge(
        customers[["customer_id", "customer_unique_id"]], on="customer_id", how="left", validate="many_to_one"
    )
    validos = base[base["pedido_valido_receita"] == 1].copy()

    if config.RECOMPRA_REGRA == "dia_diferente":
        # rank denso da DATA da compra dentro de cada cliente: 1º dia = 1,
        # 2º dia distinto = 2... Dois pedidos no mesmo dia recebem o mesmo número.
        dia = validos["order_purchase_timestamp"].dt.normalize()
        validos["numero_pedido_cliente"] = dia.groupby(validos["customer_unique_id"]).rank(method="dense")
    elif config.RECOMPRA_REGRA == "qualquer_pedido":
        validos = validos.sort_values(
            ["customer_unique_id", "order_purchase_timestamp", "order_id"], kind="stable"
        )
        validos["numero_pedido_cliente"] = validos.groupby("customer_unique_id").cumcount() + 1
    else:
        raise ValueError(f"RECOMPRA_REGRA desconhecida: {config.RECOMPRA_REGRA!r}")

    base = base.merge(
        validos[["order_id", "numero_pedido_cliente"]], on="order_id", how="left", validate="one_to_one"
    )
    base["numero_pedido_cliente"] = base["numero_pedido_cliente"].astype("Int64")
    base["cliente_recorrente"] = (base["numero_pedido_cliente"] > 1).astype("Int64")

    clientes = validos["customer_unique_id"].nunique()
    recorrentes = base.loc[base["cliente_recorrente"] == 1, "customer_unique_id"].nunique()
    log.info(
        "recorrência (regra '%s') -> %s clientes com pedido válido | %s recorrentes (%.2f%%)",
        config.RECOMPRA_REGRA, f"{clientes:,}", f"{recorrentes:,}", recorrentes / clientes * 100,
    )

    return base[["order_id", "numero_pedido_cliente", "cliente_recorrente"]]


def agregar_payments(payments: pd.DataFrame) -> pd.DataFrame:
    return (
        payments.groupby("order_id")
        .agg(
            valor_pago=("payment_value", "sum"),
            max_parcelas=("payment_installments", "max"),
            pagamento_parcelado=("pagamento_parcelado", "max"),
            pagamento_cartao=("pagamento_cartao", "max"),
            qtd_formas_pagamento=("qtd_formas_pagamento", "first"),
        )
        .reset_index()
    )


def agregar_reviews(reviews: pd.DataFrame) -> pd.DataFrame:
    return (
        reviews.groupby("order_id")
        .agg(
            review_score=("review_score", "last"),
            review_positivo=("review_positivo", "max"),
            review_negativo=("review_negativo", "max"),
            review_comentario=("possui_comentario", "max"),
        )
        .reset_index()
    )


def categoria_por_pedido(items: pd.DataFrame, products: pd.DataFrame) -> pd.DataFrame:
    """Decide a categoria 'representante' de cada pedido conforme
    `config.CATEGORIA_PEDIDO_ESTRATEGIA`. Isso é uma escolha de negócio
    (proxy), não um fato -- por isso fica isolada aqui e documentada em
    config.py em vez de embutida silenciosamente dentro de outra função.

    Empates (mesmo preço / mesma quantidade em categorias diferentes) são
    desfeitos pela ordem alfabética da categoria, para o resultado não
    depender da ordem das linhas.

    Retorna `product_category_name` (chave técnica) e `categoria_exibicao`
    (nome pronto para rótulo/gráfico) -- não retorna mais `category_en`."""
    base = items.merge(
        products[["product_id", "product_category_name", "categoria_exibicao"]],
        on="product_id", how="left", validate="many_to_one",
    )

    if config.CATEGORIA_PEDIDO_ESTRATEGIA == "quantidade":
        contagem = (
            # observed=True: com colunas 'category', o padrão do pandas 2.x
            # gera TODAS as combinações de categorias (produto cartesiano).
            base.groupby(["order_id", "product_category_name", "categoria_exibicao"], observed=True)
            .size().reset_index(name="qtd")
        )
        categoria = (
            contagem.sort_values(["order_id", "qtd", "product_category_name"],
                                 ascending=[True, False, True], kind="stable")
            .drop_duplicates(subset="order_id", keep="first")
            [["order_id", "product_category_name", "categoria_exibicao"]]
        )
    else:  # 'valor' (padrão)
        categoria = (
            base.sort_values(["order_id", "price", "product_category_name"],
                             ascending=[True, False, True], kind="stable")
            .drop_duplicates(subset="order_id", keep="first")
            [["order_id", "product_category_name", "categoria_exibicao"]]
        )

    log.info("categoria_por_pedido -> estratégia='%s'", config.CATEGORIA_PEDIDO_ESTRATEGIA)
    return categoria


def preparar_dataset_final(
    orders_fe: pd.DataFrame,
    customers: pd.DataFrame,
    reviews: pd.DataFrame,
    payments: pd.DataFrame,
    items: pd.DataFrame,
    products: pd.DataFrame,
) -> pd.DataFrame:
    """Monta o fato de pedidos (`dataset_analitico`, grão = 1 linha por
    order_id). `customer_id` já vem de `orders_fe` e funciona como FK para
    `dim_cliente` -- não denormalizamos mais cidade/estado/CEP do cliente
    aqui dentro; quem precisar desses atributos busca via relacionamento no
    Power BI. `customers` ainda é usado internamente por
    `calcular_recorrencia` (para achar o `customer_unique_id` por trás do
    `customer_id` de cada pedido), mas seus atributos descritivos não são
    copiados para o fato.

    Cada merge declara `validate="one_to_one"`: se algum lado deixar de ter
    1 linha por pedido, o pandas falha aqui, em vez de duplicar pedidos em
    silêncio."""
    reviews_agg = agregar_reviews(reviews)
    payments_agg = agregar_payments(payments)
    categoria_pedido = categoria_por_pedido(items, products)
    recorrencia = calcular_recorrencia(orders_fe, customers)

    dataset = (
        orders_fe
        .merge(reviews_agg, on="order_id", how="left", validate="one_to_one")
        .merge(payments_agg, on="order_id", how="left", validate="one_to_one")
        .merge(categoria_pedido, on="order_id", how="left", validate="one_to_one")
        .merge(recorrencia, on="order_id", how="left", validate="one_to_one")
    )
    dataset["comentario_exibicao"] = (
    dataset["review_comentario"].map({1: "Com comentário", 0: "Sem comentário"}).fillna("Sem avaliação")
    )

    # review_score pode ficar ausente para pedidos sem review (left join) --
    # forçamos float64 explicitamente para não depender do acaso de a amostra
    # ter ou não nulos (int64 vs float64 muda conforme os dados de entrada).
    dataset["review_score"] = dataset["review_score"].astype("float64")

    log.info("dataset final (fato pedidos) -> %s linhas | %s pedidos únicos",
              f"{dataset.shape[0]:,}", f"{dataset['order_id'].nunique():,}")

    return dataset
