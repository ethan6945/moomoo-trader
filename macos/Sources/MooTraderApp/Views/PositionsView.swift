import SwiftUI

/// Open positions — the web dashboard's 持仓 table, natively.
struct PositionsView: View {
    let positions: [Position]

    var body: some View {
        if positions.isEmpty {
            Text("🌙 当前无持仓")
                .foregroundStyle(.secondary)
                .frame(maxWidth: .infinity, alignment: .center)
                .padding(.vertical, 24)
        } else {
            Table(positions) {
                TableColumn("代码") { p in
                    Text(p.symbol).fontWeight(.semibold)
                }
                .width(min: 62, ideal: 70)

                TableColumn("数量") { p in Text(Fmt.qty(p.qty)) }
                    .width(min: 44, ideal: 52)

                TableColumn("入场") { p in Text(Fmt.money(p.entryPrice)) }
                    .width(min: 62, ideal: 74)

                TableColumn("现价") { p in Text(Fmt.money(p.last)) }
                    .width(min: 62, ideal: 74)

                TableColumn("止损") { p in
                    Text(Fmt.money(p.stopLoss)).foregroundStyle(.red)
                }
                .width(min: 62, ideal: 74)

                TableColumn("止盈") { p in
                    Text(Fmt.money(p.takeProfit)).foregroundStyle(.green)
                }
                .width(min: 62, ideal: 74)

                TableColumn("区间") { p in RangeBar(position: p) }
                    .width(min: 70, ideal: 110)

                TableColumn("策略") { p in StrategyTags(position: p) }
                    .width(min: 90, ideal: 150)

                TableColumn("盈亏") { p in
                    if let v = p.plValue, p.last != nil {
                        Text("\(Fmt.signed(v)) (\(Fmt.pct(p.plRatio)))")
                            .foregroundStyle(pnlColor(v))
                    } else {
                        Text("—")
                    }
                }
                .width(min: 110, ideal: 140)
            }
            .frame(minHeight: 150, idealHeight: max(150, Double(positions.count) * 28 + 40))
        }
    }
}

/// Strategy tag + chart pattern + manual-adoption badge, matching the web UI's
/// semantics: 手动·自管 = your own buy, high risk, you're watching it;
/// 手动·接管 = the bot has taken over stops/targets.
struct StrategyTags: View {
    let position: Position

    var body: some View {
        HStack(spacing: 4) {
            if position.manualAdopted {
                Pill(text: position.userManaged ? "手动·自管" : "手动·接管",
                     tint: position.userManaged ? .orange : .blue, icon: "🖐")
                    .help(position.userManaged
                          ? "你手动买入 · 高风险 · 等你确认是否接管（你自己盯盘）"
                          : "你手动买入 · 已被机器人接管止损/止盈")
            } else {
                Pill(text: position.strategy ?? "—")
            }
            if let pattern = position.pattern {
                Pill(text: pattern, tint: .purple, icon: "📐").help("AI 形态识别")
            }
        }
    }
}
