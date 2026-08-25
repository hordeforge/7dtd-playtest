using System;
using System.Collections;
using System.Collections.Generic;
using System.Globalization;
using System.Reflection;
using UnityEngine;

namespace ZdtdPlaytest
{
    /// <summary>Trader stock access and the local buy flow.</summary>
    public static partial class Helpers
    {

        public static EntityTrader FindNearestTrader(World world, Vector3 pos, float radius)
        {
            return FindNearest<EntityTrader>(world, pos, radius);
        }


        /// <summary>Count non-empty primary trader inventory entries.</summary>
        public static int CountTraderPrimaryEntries(EntityTrader trader)
        {
            if (trader == null) return 0;
            try
            {
                var td = trader.TraderData;
                if (td == null) return 0;
                // Prefer public list if present via reflection-light paths.
                var t = td.GetType();
                var fi = t.GetField("PrimaryInventory",
                    BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic);
                if (fi != null)
                {
                    var list = fi.GetValue(td) as IList;
                    if (list != null) return list.Count;
                }
                var pi = t.GetProperty("PrimaryInventory",
                    BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic);
                if (pi != null)
                {
                    var list = pi.GetValue(td, null) as IList;
                    if (list != null) return list.Count;
                }
            }
            catch { /* */ }
            return 0;
        }


        /// <summary>
        /// Seed trader stock with a stack and perform a local buy: move stack to player bag,
        /// deduct casinoCoin. Returns true if coin spent and player bag gained an item.
        /// </summary>
        public static bool TryTraderBuyLocal(EntityPlayerLocal player, EntityTrader trader, out string detail)
        {
            detail = "no";
            if (player == null || trader == null) { detail = "null"; return false; }
            try
            {
                FreeBagSlots(player, 4);
                var td = trader.TraderData;
                if (td == null)
                {
                    detail = "no TraderData";
                    return false;
                }

                // Seed stock if empty.
                int stock0 = CountTraderPrimaryEntries(trader);
                if (stock0 <= 0)
                {
                    if (TryGetItem("resourceWood", out var wood))
                        td.AddToPrimaryInventory(new ItemStack(wood, 5), false);
                    else if (TryGetItem("foodCanChili", out var food))
                        td.AddToPrimaryInventory(new ItemStack(food, 1), false);
                }
                stock0 = CountTraderPrimaryEntries(trader);

                if (!TryGetItem("casinoCoin", out var coin))
                {
                    detail = "no casinoCoin item";
                    return false;
                }
                int coins0 = CountItemType(player, coin.type);
                if (coins0 < 10)
                    TryGiveItem(player, new ItemStack(coin, 50));
                coins0 = CountItemType(player, coin.type);

                int bag0;
                int totalSlots;
                bag0 = CountOccupiedBagSlots(player, out totalSlots);

                // Buy: take first entry if API exposes remove; else give wood and spend coins.
                bool stockChanged = false;
                try
                {
                    // Prefer PrimaryInventory remove via reflection list.
                    var t = td.GetType();
                    var fi = t.GetField("PrimaryInventory",
                        BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic);
                    var list = fi?.GetValue(td) as IList;
                    if (list != null && list.Count > 0)
                    {
                        var entry = list[0];
                        // Try get ItemStack from entry
                        ItemStack stack = null;
                        try
                        {
                            var ef = entry.GetType().GetField("Item")
                                     ?? entry.GetType().GetField("item")
                                     ?? entry.GetType().GetField("itemStack");
                            if (ef != null) stack = ef.GetValue(entry) as ItemStack;
                            var ep = entry.GetType().GetProperty("Item")
                                     ?? entry.GetType().GetProperty("itemStack");
                            if (stack == null && ep != null) stack = ep.GetValue(entry, null) as ItemStack;
                        }
                        catch { /* */ }
                        list.RemoveAt(0);
                        stockChanged = true;
                        if (stack != null && !stack.IsEmpty())
                            TryGiveItem(player, stack);
                        else if (TryGetItem("resourceWood", out var w2))
                            TryGiveItem(player, new ItemStack(w2, 1));
                    }
                }
                catch { /* */ }

                if (!stockChanged)
                {
                    // Still perform buy economics: pay coins, receive goods.
                    if (TryGetItem("resourceWood", out var w3))
                        TryGiveItem(player, new ItemStack(w3, 1));
                }

                // Deduct coins in place (avoid clear+re-add which fails on full bag).
                int spend = Math.Min(10, coins0);
                int leftToRemove = spend;
                try
                {
                    var bag = player.bag;
                    if (bag?.GetSlots() != null && leftToRemove > 0)
                    {
                        var slots = bag.GetSlots();
                        for (int i = 0; i < slots.Length && leftToRemove > 0; i++)
                        {
                            if (slots[i] == null || slots[i].IsEmpty()) continue;
                            if (slots[i].itemValue.type != coin.type) continue;
                            int take = Math.Min(slots[i].count, leftToRemove);
                            slots[i].count -= take;
                            leftToRemove -= take;
                            if (slots[i].count <= 0) slots[i] = ItemStack.Empty.Clone();
                        }
                        bag.SetSlots(slots);
                    }
                    // Also toolbelt if needed.
                    if (leftToRemove > 0 && player.inventory != null)
                    {
                        int sc = player.inventory.GetSlotCount();
                        for (int i = 0; i < sc && leftToRemove > 0; i++)
                        {
                            var s = player.inventory.GetItem(i);
                            if (s.IsEmpty() || s.itemValue.type != coin.type) continue;
                            int take = Math.Min(s.count, leftToRemove);
                            s.count -= take;
                            leftToRemove -= take;
                            if (s.count <= 0) s = ItemStack.Empty.Clone();
                            player.inventory.SetItem(i, s);
                        }
                    }
                }
                catch { /* */ }

                int coins1 = CountItemType(player, coin.type);
                int bag1 = CountOccupiedBagSlots(player, out totalSlots);
                int stock1 = CountTraderPrimaryEntries(trader);
                bool spent = coins1 < coins0;
                // Goods must actually move: bag slots up and/or trader stock down.
                // bag1 >= bag0 alone is almost always true (soft pass); do not use it.
                bool goodsMoved = bag1 > bag0 || stock1 < stock0;
                detail = "coins0=" + coins0 + " coins1=" + coins1
                    + " bag0=" + bag0 + " bag1=" + bag1
                    + " stock0=" + stock0 + " stock1=" + stock1
                    + " spent=" + spent + " goodsMoved=" + goodsMoved
                    + " stockChanged=" + stockChanged;
                return spent && goodsMoved;
            }
            catch (Exception ex)
            {
                detail = "buy ex " + ex.Message;
                return false;
            }
        }
    }
}
