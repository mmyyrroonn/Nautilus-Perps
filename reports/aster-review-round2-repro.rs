// Append these functions to a copy of the current Aster tests/exec_client.rs harness.
// See aster-review-round2-2026-09-05.md for the offline run command.

#[rstest]
#[tokio::test]
async fn review_round2_reconnect_preserves_real_trade_economics() {
    let venue = MockVenue::start().await;
    let mut harness = connected_harness(&venue).await;
    let order = limit_order("O-REVIEW-GAP", OrderSide::Buy, false);
    submit_and_settle(&harness, &order, &venue).await;
    tokio::time::sleep(Duration::from_millis(100)).await;
    drain_exec(&mut harness.exec_rx);
    let trade_ms = now_ms();
    venue.script(|s| {
        let mut row = venue_order(900200, "O-REVIEW-GAP", "BTCUSDT", "FILLED", "BUY");
        row["time"] = json!(trade_ms - 1);
        row["updateTime"] = json!(trade_ms);
        s.orders.insert("O-REVIEW-GAP".to_string(), row.clone());
        s.orders.insert("900200".to_string(), row);
        s.open_orders = json!([]);
        s.user_trades.insert("BTCUSDT".to_string(), vec![venue_trade(7001,900200,"BTCUSDT",trade_ms,"0.02")]);
    });
    venue.drop_ws();
    wait_until_async(|| async { venue.ws_connection_count() >= 2 }, Duration::from_secs(20)).await;
    wait_until_async(|| async { !venue.requests_for("GET", "userTrades").is_empty() }, Duration::from_secs(20)).await;
    tokio::time::sleep(Duration::from_millis(600)).await;
    let events = drain_exec(&mut harness.exec_rx);
    assert!(fill_trade_ids(&events).contains(&"7001".to_string()), "{events:?}");
    let cache = Rc::new(RefCell::new(Cache::default()));
    seed_account(&cache);
    while let Ok(event) = harness.data_rx.try_recv() {
        if let DataEvent::Instrument(i) = event { cache.borrow_mut().add_instrument(i).unwrap(); }
    }
    let mut engine = ExecutionEngine::new(Rc::new(RefCell::new(TestClock::new())), cache.clone(), None);
    engine.register_client(Box::new(harness.client)).unwrap();
    engine.register_oms_type(StrategyId::from("EXTERNAL"), OmsType::Netting);
    for event in &events { if let ExecutionEvent::Report(r) = event { engine.reconcile_execution_report(r); } }
    let c = cache.borrow();
    let o = c.order(&ClientOrderId::from("O-REVIEW-GAP")).unwrap();
    assert_eq!(o.filled_qty(), Quantity::from("0.010"));
    assert!(o.trade_ids().iter().any(|id| id.as_str() == "7001"), "real trade lost: ids={:?}, fees={:?}", o.trade_ids(), o.commissions());
    assert_eq!(o.commissions().get(&Currency::USDT()), Some(&Money::from("0.02 USDT")));
}

#[rstest]
#[tokio::test]
async fn review_round2_mass_status_keeps_fill_for_order_created_before_window() {
    let venue = MockVenue::start().await;
    let harness = connected_harness(&venue).await;
    let time = now_ms();
    venue.script(|s| {
        let mut row = venue_order(920001,"O-OLD-GTC","BTCUSDT","FILLED","BUY");
        row["time"] = json!(time - 120_000);
        row["updateTime"] = json!(time - 1_000);
        s.all_orders.insert("BTCUSDT".to_string(),vec![row.clone()]);
        s.orders.insert("920001".to_string(),row);
        s.user_trades.insert("BTCUSDT".to_string(),vec![venue_trade(920101,920001,"BTCUSDT",time-1_000,"0.02")]);
        s.position_risk = json!([{"symbol":"BTCUSDT","positionAmt":"0.010","entryPrice":"50000.00","positionSide":"BOTH","updateTime":time}]);
    });
    let mass = harness.client.generate_mass_status(Some(1)).await.unwrap().unwrap();
    let ids: Vec<String> = mass.fill_reports().values().flat_map(|rows| rows.iter().map(|r|r.trade_id.to_string())).collect();
    assert!(ids.contains(&"920101".to_string()), "in-window real trade discarded; complete={}, orders={}, fills={ids:?}",mass.reports_complete(),mass.order_reports().len());
}

#[rstest]
#[tokio::test]
async fn review_round2_structured_503_must_not_reject_live_order() {
    let venue = MockVenue::start().await;
    let mut harness = connected_harness(&venue).await;
    venue.script(|s| {
        s.submit = SubmitOutcome::Status {
            status: 503,
            body: json!({"code": -1000, "msg": "An unknown error occured while processing the request."}).to_string(),
        };
        s.orders.insert("O-503-JSON".to_string(),
            venue_order(930001,"O-503-JSON","BTCUSDT","NEW","BUY"));
    });
    let order = limit_order("O-503-JSON", OrderSide::Buy, false);
    drain_exec(&mut harness.exec_rx);
    submit_and_settle(&harness, &order, &venue).await;
    tokio::time::sleep(Duration::from_millis(300)).await;
    let events = drain_exec(&mut harness.exec_rx);
    assert!(!order_events(&events).iter().any(|e| matches!(e, OrderEventAny::Rejected(_))),
        "structured HTTP503 terminalised a potentially live order: {events:?}");
}

#[rstest]
#[tokio::test]
async fn review_round2_failed_fill_report_does_not_consume_recovery_trade() {
    let venue = MockVenue::start().await;
    let mut harness = connected_harness(&venue).await;
    drain_exec(&mut harness.exec_rx);
    let trade_ms = now_ms();
    venue.script(|s| {
        s.user_trades.insert("BTCUSDT".to_string(), vec![
            venue_trade(930101, 930001, "BTCUSDT", trade_ms, "0.02"),
            venue_trade(930102, 930002, "BTCUSDT", trade_ms, "not-a-number"),
        ]);
        let mut row = venue_order(930001, "O-REPORT-RETRY", "BTCUSDT", "FILLED", "BUY");
        row["time"] = json!(trade_ms);
        row["updateTime"] = json!(trade_ms);
        s.orders.insert("930001".to_string(), row);
        s.open_orders = json!([]);
    });
    let error = harness.client.generate_fill_reports(recent_fill_reports_command()).await
        .expect_err("invalid second commission must fail the whole report request");
    assert!(format!("{error:#}").contains("commission"), "{error:#}");
    assert!(fill_trade_ids(&drain_exec(&mut harness.exec_rx)).is_empty());
    venue.script(|s| {
        s.user_trades.insert("BTCUSDT".to_string(), vec![
            venue_trade(930101, 930001, "BTCUSDT", trade_ms, "0.02"),
        ]);
    });
    venue.drop_ws();
    wait_until_async(|| async { venue.ws_connection_count() >= 2 }, Duration::from_secs(20)).await;
    wait_until_async(|| async {
        venue.requests_for("GET", "userTrades").iter()
            .filter(|r| r.param("symbol") == Some("BTCUSDT")).count() >= 2
    }, Duration::from_secs(20)).await;
    tokio::time::sleep(Duration::from_millis(600)).await;
    let events = drain_exec(&mut harness.exec_rx);
    assert!(fill_trade_ids(&events).contains(&"930101".to_string()),
        "failed report request must not consume a trade that the engine never received: {events:?}");
}

#[rstest]
#[tokio::test]
async fn review_round2_data_request_preserves_verified_fees() {
    use nautilus_aster::config::AsterDataClientConfig;
    use nautilus_binance::{common::enums::BinanceProductType, futures::data::BinanceFuturesDataClient};
    use nautilus_common::{clients::DataClient, messages::data::{DataResponse, RequestInstruments}};
    let venue = MockVenue::start().await;
    let mut harness = connected_harness(&venue).await;
    while let Ok(event) = harness.data_rx.try_recv() {
        if let DataEvent::Instrument(i) = event { harness.cache.borrow_mut().add_instrument(i).unwrap(); }
    }
    let before = harness.cache.borrow().instrument(&InstrumentId::from(BTC)).unwrap().taker_fee();
    assert_eq!(before, ASTER_TAKER.parse().unwrap());
    let config = AsterDataClientConfig {
        base_url_http: Some(venue.http_url()),
        base_url_ws: Some(venue.ws_url()),
        ..Default::default()
    };
    let mut data = BinanceFuturesDataClient::new(*ASTER_CLIENT_ID, config.to_binance(), BinanceProductType::UsdM).unwrap();
    data.start().unwrap();
    data.request_instruments(RequestInstruments::new(None,None,Some(*ASTER_CLIENT_ID),Some(*ASTER_VENUE),UUID4::new(),UnixNanos::default(),None)).unwrap();
    let response = tokio::time::timeout(Duration::from_secs(3), async {
        loop {
            if let Some(DataEvent::Response(DataResponse::Instruments(r))) = harness.data_rx.recv().await { break r; }
        }
    }).await.expect("instrument response must arrive");
    for instrument in response.data {
        harness.cache.borrow_mut().add_instrument(instrument).unwrap();
    }
    let after = harness.cache.borrow().instrument(&InstrumentId::from(BTC)).unwrap().taker_fee();
    data.stop().unwrap();
    assert_eq!(after, before, "data request overwrote verified account taker fee");
}
