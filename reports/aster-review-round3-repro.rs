#[rstest]
#[tokio::test]
async fn review_round3_failed_fill_compensation_preserves_eventual_real_economics() {
    let venue = MockVenue::start().await;
    let mut harness = connected_harness(&venue).await;
    let order = limit_order("O-R3-TRANSIENT", OrderSide::Buy, false);
    submit_and_settle(&harness, &order, &venue).await;
    tokio::time::sleep(Duration::from_millis(100)).await;
    drain_exec(&mut harness.exec_rx);
    let trade_ms = now_ms();
    venue.script(|s| {
        let mut row = venue_order(950001, "O-R3-TRANSIENT", "BTCUSDT", "FILLED", "BUY");
        row["time"] = json!(trade_ms - 1);
        row["updateTime"] = json!(trade_ms);
        s.orders.insert("O-R3-TRANSIENT".to_string(), row.clone());
        s.orders.insert("950001".to_string(), row);
        s.open_orders = json!([]);
        s.user_trades.insert("BTCUSDT".to_string(), vec![venue_trade(950101, 950001, "BTCUSDT", trade_ms, "0.02")]);
        s.user_trades_error.insert("BTCUSDT".to_string(), json!({"code": -1000, "msg": "temporary history failure"}));
    });
    venue.drop_ws();
    wait_until_async(|| async { venue.ws_connection_count() >= 2 }, Duration::from_secs(20)).await;
    wait_until_async(|| async { !venue.requests_for("GET", "openOrders").is_empty() }, Duration::from_secs(20)).await;
    tokio::time::sleep(Duration::from_millis(600)).await;
    let mut events = drain_exec(&mut harness.exec_rx);
    venue.script(|s| { s.user_trades_error.clear(); });
    venue.clear_requests();
    venue.drop_ws();
    wait_until_async(|| async { venue.ws_connection_count() >= 3 }, Duration::from_secs(20)).await;
    wait_until_async(|| async { !venue.requests_for("GET", "openOrders").is_empty() }, Duration::from_secs(20)).await;
    tokio::time::sleep(Duration::from_millis(600)).await;
    events.extend(drain_exec(&mut harness.exec_rx));
    assert!(fill_trade_ids(&events).contains(&"950101".to_string()), "recovered real fill must reach the engine: {events:?}");
    let cache = Rc::new(RefCell::new(Cache::default()));
    seed_account(&cache);
    while let Ok(event) = harness.data_rx.try_recv() {
        if let DataEvent::Instrument(i) = event { cache.borrow_mut().add_instrument(i).unwrap(); }
    }
    let mut engine = ExecutionEngine::new(Rc::new(RefCell::new(TestClock::new())), cache.clone(), None);
    engine.register_client(Box::new(harness.client)).unwrap();
    engine.register_oms_type(StrategyId::from("EXTERNAL"), OmsType::Netting);
    for event in &events {
        if let ExecutionEvent::Report(r) = event { engine.reconcile_execution_report(r); }
    }
    let c = cache.borrow();
    let o = c.order(&ClientOrderId::from("O-R3-TRANSIENT")).unwrap();
    assert_eq!(o.filled_qty(), Quantity::from("0.010"));
    assert!(o.trade_ids().iter().any(|id| id.as_str() == "950101"), "temporary fill-history failure permanently replaced real trade: ids={:?}, fees={:?}", o.trade_ids(), o.commissions());
    assert_eq!(o.commissions().get(&Currency::USDT()), Some(&Money::from("0.02 USDT")));
}

#[rstest]
#[tokio::test]
async fn review_round3_unlinked_startup_fill_remains_recoverable() {
    let venue = MockVenue::start().await;
    let mut harness = connected_harness(&venue).await;
    drain_exec(&mut harness.exec_rx);
    let trade_ms = now_ms();
    venue.script(|s| {
        s.user_trades.insert("BTCUSDT".to_string(), vec![venue_trade(960101, 960001, "BTCUSDT", trade_ms, "0.02")]);
        s.position_risk = json!([{"symbol":"BTCUSDT","positionAmt":"0.010","entryPrice":"50000.00","positionSide":"BOTH","updateTime":trade_ms}]);
    });
    let mass = harness.client.generate_mass_status(Some(1)).await.unwrap().unwrap();
    assert!(!mass.reports_complete());
    assert_eq!(mass.fill_reports().values().flatten().count(), 1);
    let cache = Rc::new(RefCell::new(Cache::default()));
    seed_account(&cache);
    while let Ok(event) = harness.data_rx.try_recv() {
        if let DataEvent::Instrument(i) = event { cache.borrow_mut().add_instrument(i).unwrap(); }
    }
    let clock = Rc::new(RefCell::new(TestClock::new()));
    let mut manager = ExecutionManager::new(clock.clone(), cache.clone(), ExecutionManagerConfig::default()).unwrap();
    let engine = Rc::new(RefCell::new(ExecutionEngine::new(clock, cache.clone(), None)));
    engine.borrow_mut().register_client(Box::new(harness.client)).unwrap();
    engine.borrow_mut().register_oms_type(StrategyId::from("EXTERNAL"), OmsType::Netting);
    manager.reconcile_execution_mass_status(mass, engine.clone()).await;
    assert!(cache.borrow().order(&ClientOrderId::from("O-R3-UNLINKED")).is_none());
    venue.script(|s| {
        let mut row = venue_order(960001, "O-R3-UNLINKED", "BTCUSDT", "FILLED", "BUY");
        row["time"] = json!(trade_ms - 120_000);
        row["updateTime"] = json!(trade_ms);
        s.orders.insert("960001".to_string(), row);
    });
    venue.clear_requests();
    venue.drop_ws();
    wait_until_async(|| async { venue.ws_connection_count() >= 2 }, Duration::from_secs(20)).await;
    wait_until_async(|| async { !venue.requests_for("GET", "openOrders").is_empty() }, Duration::from_secs(20)).await;
    tokio::time::sleep(Duration::from_millis(600)).await;
    let events = drain_exec(&mut harness.exec_rx);
    assert!(fill_trade_ids(&events).contains(&"960101".to_string()), "unlinked fill was skipped by startup reconciliation and must remain recoverable after its order becomes available: {events:?}");
}

#[rstest]
#[tokio::test]
async fn review_round3_account_fee_registry_leaks_between_clients() {
    use nautilus_binance::{common::enums::{BinanceEnvironment, BinanceProductType}, futures::http::client::BinanceFuturesHttpClient};
    use nautilus_core::time::get_atomic_clock_realtime;
    let fee_venue = Venue::from("ASTERROUND3FEES");
    let build = |mock: &MockVenue, account: &str| {
        let account_id = AccountId::from(account);
        let cache = Rc::new(RefCell::new(Cache::default()));
        let core = ExecutionClientCore::new(
            TraderId::from("TESTER-001"), *ASTER_CLIENT_ID, fee_venue,
            OmsType::Netting, account_id, AccountType::Margin, None, cache.clone(),
        );
        let config = AsterExecutionClientConfig {
            account_id,
            signer_private_key: Some(TEST_PRIVATE_KEY.to_string()),
            base_url_http: Some(mock.http_url()),
            base_url_ws: Some(mock.ws_url()),
            http_timeout_secs: Some(30),
            venue: Some(fee_venue),
            ..Default::default()
        };
        let (exec_tx, exec_rx) = tokio::sync::mpsc::unbounded_channel();
        let (data_tx, data_rx) = tokio::sync::mpsc::unbounded_channel();
        replace_exec_event_sender(exec_tx);
        replace_data_event_sender(data_tx);
        Harness { client: AsterExecutionClient::new(core, config).unwrap(), exec_rx, data_rx, cache }
    };
    let first_mock = MockVenue::start().await;
    script_connect(&first_mock);
    let mut first = build(&first_mock, "ASTER-ROUND3-A");
    first.client.start().unwrap();
    first.client.connect().await.unwrap();
    let data = BinanceFuturesHttpClient::new(BinanceProductType::UsdM, BinanceEnvironment::Live,
        get_atomic_clock_realtime(), None, None, Some(first_mock.http_url()), None,
        Some(30), None, false).unwrap().with_venue(fee_venue);
    let load = nautilus_binance::config::BinanceInstrumentProviderConfig::default();
    let before = data.request_instruments_with_config(&load).await.unwrap().into_iter()
        .find(|i| i.raw_symbol().as_str() == "BTCUSDT").unwrap().taker_fee();
    assert_eq!(before, ASTER_TAKER.parse().unwrap());
    let second_mock = MockVenue::start().await;
    script_connect(&second_mock);
    second_mock.script(|s| {
        s.commission_rates.get_mut("BTCUSDT").unwrap()["takerCommissionRate"] = json!("0.001000");
    });
    let mut second = build(&second_mock, "ASTER-ROUND3-B");
    second.client.start().unwrap();
    second.client.connect().await.unwrap();
    let after = data.request_instruments_with_config(&load).await.unwrap().into_iter()
        .find(|i| i.raw_symbol().as_str() == "BTCUSDT").unwrap().taker_fee();
    first.client.stop().unwrap();
    second.client.stop().unwrap();
    nautilus_binance::common::fees::clear_instrument_fees(fee_venue);
    assert_eq!(after, before, "first endpoint acquired the second account's fees");
}
