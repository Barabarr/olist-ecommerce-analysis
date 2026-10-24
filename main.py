"""Orquestrador do pipeline

Saída (etl/config.PROCESSED):
    inspecao.csv                     perfil das tabelas brutas (nulos, duplicatas, chaves)
    integridade.csv                  integridade referencial entre as tabelas brutas
    dim_data.parquet                 calendário (anos completos)
    dim_cliente.parquet              quem comprou
    dim_geo_cliente.parquet          geolocalização do cliente
    dataset_analitico.parquet        fato, grão = pedido (TODOS os pedidos; ver pedido_valido_receita)
    fato_itens.parquet               fato, grão = item
    fato_pagamentos.parquet          fato, grão = forma de pagamento do pedido
    dim_produtos.parquet             catálogo de produtos
    dim_sellers.parquet              vendedores
    dim_geo_vendedor.parquet         geolocalização do vendedor
    reviews_nlp.parquet              texto da avaliação escolhida por pedido (apoio p/ NLP)
    fato_palavras_review.parquet     1 linha por ocorrência de palavra (apoio p/ NLP)
    run_manifest.json                auditoria da execução (gravado mesmo se a execução falhar)

Se a validação falhar, nada é exportado e o manifesto registra status
"falhou" -- os parquets em disco continuam sendo os da última execução bem
sucedida, e o manifesto diz isso.
"""
from __future__ import annotations

import json
import platform
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from etl import config, features, limpeza, modelagem, validacao
from etl.io import carregar_dados, inspecionar_dados, inspecionar_integridade
from etl.logger import get_logger

log = get_logger(__name__)


def _json_default(obj):
    """Converte tipos numpy/pandas para JSON (números continuam números)."""
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.floating):
        return float(obj)
    return str(obj)


def _metricas(dataset: pd.DataFrame, customers: pd.DataFrame) -> dict:
    """Métricas-resumo da execução, cada uma com o grão explícito no nome."""
    validos = dataset[dataset["pedido_valido_receita"] == 1].merge(
        customers[["customer_id", "customer_unique_id"]],
        on="customer_id", how="left", validate="one_to_one",
    )
    clientes = validos["customer_unique_id"].nunique()
    recorrentes = validos.loc[validos["numero_pedido_cliente"] > 1, "customer_unique_id"].nunique()
    sem_receita = dataset["pedido_valido_receita"] == 0
    return {
        "pedidos_total": len(dataset),
        "pedidos_validos_receita": len(validos),
        "pedidos_sem_receita": int(sem_receita.sum()),
        "receita_produtos_pedidos_validos": round(float(validos["valor_produtos"].sum()), 2),
        "receita_produtos_pedidos_sem_receita": round(float(dataset.loc[sem_receita, "valor_produtos"].sum()), 2),
        "pedidos_sem_itens": int(dataset["pedido_sem_itens"].sum()),
        "pedidos_datas_inconsistentes": int(dataset["flag_datas_inconsistentes"].sum()),
        "regra_recompra": config.RECOMPRA_REGRA,
        "clientes_com_pedido_valido": clientes,
        "clientes_recorrentes": recorrentes,
        "pct_clientes_recorrentes": round(recorrentes / clientes * 100, 2),
        "pct_pedidos_que_sao_recompra": round(float((validos["cliente_recorrente"] == 1).mean()) * 100, 2),
    }


def main() -> None:
    manifesto: dict = {
        "inicio": datetime.now(timezone.utc).isoformat(),
        "status": "em_execucao",
        "versoes": {
            "python": platform.python_version(),
            "pandas": pd.__version__,
            "numpy": np.__version__,
        },
        "config": {
            "STATUS_SEM_RECEITA": sorted(config.STATUS_SEM_RECEITA),
            "RECOMPRA_REGRA": config.RECOMPRA_REGRA,
            "CATEGORIA_PEDIDO_ESTRATEGIA": config.CATEGORIA_PEDIDO_ESTRATEGIA,
        },
        "etapas": {},
    }
    config.PROCESSED.mkdir(parents=True, exist_ok=True)

    try:
        dfs = carregar_dados(config.RAW)

        log.info("Resumo geral")
        inspecao_df = inspecionar_dados(dfs)
        inspecao_df.to_csv(config.PROCESSED / "inspecao.csv", index=False)
        integridade_df = inspecionar_integridade(dfs)
        integridade_df.to_csv(config.PROCESSED / "integridade.csv", index=False)

        log.info("Tratamento dos dados")
        orders, manifesto["etapas"]["orders"] = limpeza.tratar_orders(dfs["orders"])
        geo, manifesto["etapas"]["geo"] = limpeza.tratar_geo(dfs["geo"])
        products = limpeza.tratar_products(dfs["products"])
        items = limpeza.tratar_items(dfs["items"])
        reviews, reviews_nlp, manifesto["etapas"]["reviews"] = limpeza.tratar_reviews(dfs["reviews"])
        payments, manifesto["etapas"]["payments"] = limpeza.tratar_payments(dfs["payments"])
        customers = limpeza.tratar_customers(dfs["customers"])
        sellers = limpeza.tratar_sellers(dfs["sellers"])

        log.info("Modelagem dimensional")
        dim_data = modelagem.construir_dim_data(orders)
        dim_geo_cliente = modelagem.construir_dim_geo(
            geo, customers["customer_zip_code_prefix"], papel="cliente"
        )
        dim_geo_vendedor = modelagem.construir_dim_geo(
            geo, sellers["seller_zip_code_prefix"], papel="vendedor"
        )

        orders_fe = features.feature_engineering(orders, items)

        dataset_final = features.preparar_dataset_final(
            orders_fe, customers, reviews, payments, items, products
        )

        # Lead time vem do fato de pedidos final (fonte única): fatos do
        # pedido inteiro, replicados no grão de item de propósito.
        fato_itens = (
            items[["order_id", "order_item_id", "product_id", "seller_id", "price", "freight_value"]]
            .merge(
                dataset_final[["order_id", "dias_aprovacao", "dias_preparacao", "dias_transporte",
                               "dias_entrega", "dias_atraso", "pedido_atrasado"]],
                on="order_id", how="left", validate="many_to_one",
            )
        )

        fato_pagamentos = modelagem.construir_fato_pagamentos(payments)

        # Gap 2: distância cliente-vendedor (Haversine), adicionada a fato_itens
        fato_itens = modelagem.adicionar_distancia_km(fato_itens, orders, customers, sellers, geo)

        # Gap 3: tokenização de reviews (stopwords em PT) -- tabela de apoio p/
        # nuvem de palavras e "Top palavras positivas/negativas"
        fato_palavras_review = modelagem.construir_fato_palavras_review(reviews_nlp)

        saidas = {
            "dim_data": dim_data,
            "dim_cliente": customers,
            "dim_geo_cliente": dim_geo_cliente,
            "dataset_analitico": dataset_final,
            "fato_itens": fato_itens,
            "fato_pagamentos": fato_pagamentos,
            "dim_produtos": products,
            "dim_sellers": sellers,
            "dim_geo_vendedor": dim_geo_vendedor,
            "reviews_nlp": reviews_nlp,
            "fato_palavras_review": fato_palavras_review,
        }

        # --- Contrato de qualidade (falha aqui = nada é exportado) ----------
        manifesto["validacao"] = validacao.validar_modelo(saidas)

        # --- Exportação ----------------------------------------------------
        for nome, df in saidas.items():
            df.to_parquet(config.PROCESSED / f"{nome}.parquet", index=False)
            log.info("%-22s exportado (%s linhas x %s colunas)", f"{nome}.parquet",
                      f"{df.shape[0]:,}", df.shape[1])

        manifesto["saidas"] = {nome: list(df.shape) for nome, df in saidas.items()}
        manifesto["metricas"] = _metricas(dataset_final, customers)
        manifesto["status"] = "sucesso"

        log.info("clientes recorrentes: %s%% dos clientes | %s%% dos pedidos válidos são recompra",
                  manifesto["metricas"]["pct_clientes_recorrentes"],
                  manifesto["metricas"]["pct_pedidos_que_sao_recompra"])

    except Exception as exc:
        manifesto["status"] = "falhou"
        manifesto["erro"] = repr(exc)
        log.error("Execução falhou -- nenhum arquivo novo exportado: %r", exc)
        raise
    finally:
        manifesto["fim"] = datetime.now(timezone.utc).isoformat()
        with open(config.PROCESSED / "run_manifest.json", "w", encoding="utf-8") as f:
            json.dump(manifesto, f, indent=2, ensure_ascii=False, default=_json_default)
        log.info("run_manifest.json gravado (status: %s)", manifesto["status"])


if __name__ == "__main__":
    main()
