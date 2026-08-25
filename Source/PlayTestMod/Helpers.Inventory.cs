using System;
using System.Collections;
using System.Collections.Generic;
using System.Globalization;
using System.Reflection;
using UnityEngine;

namespace ZdtdPlaytest
{
    /// <summary>Bag/toolbelt item flow: resolve, give, equip, drop, consume, and craft via stock client APIs.</summary>
    public static partial class Helpers
    {

        /// <summary>
        /// Push local toolbelt+bag to server via NetPackagePlayerInventory (stock C2S).
        /// Needed after TryGiveItem so server ECS has food before InstantAction eats.
        /// </summary>
        public static bool PushPlayerInventory(EntityPlayerLocal player, out string detail)
        {
            detail = "no push";
            if (player == null) return false;
            try
            {
                var pkg = NetPackageManager.GetPackage<NetPackagePlayerInventory>();
                if (pkg == null) { detail = "null PlayerInventory pkg"; return false; }
                // Setup(player, toolbelt, bag, equipment, drag): push toolbelt+bag.
                pkg.Setup(player, true, true, false, false);
                var cm = SingletonMonoBehaviour<ConnectionManager>.Instance;
                if (cm == null) { detail = "no ConnectionManager"; return false; }
                cm.SendToServer(pkg);
                detail = "PlayerInventory pushed";
                return true;
            }
            catch (Exception ex)
            {
                detail = "push inv ex " + ex.Message;
                return false;
            }
        }


        /// <summary>Count bag slots that hold a real item (type != 0).</summary>
        public static int CountOccupiedBagSlots(EntityPlayerLocal p, out int totalSlots)
        {
            totalSlots = 0;
            try
            {
                var bag = p.bag;
                if (bag == null) return 0;
                var slots = bag.GetSlots();
                if (slots == null) return 0;
                totalSlots = slots.Length;
                int n = 0;
                for (int i = 0; i < slots.Length; i++)
                {
                    var s = slots[i];
                    if (s.itemValue.type != 0 && s.count > 0) n++;
                }
                return n;
            }
            catch
            {
                return 0;
            }
        }


        /// <summary>Resolve a vanilla item by name; empty if missing.</summary>
        public static bool TryGetItem(string name, out ItemValue iv)
        {
            iv = ItemValue.None;
            try
            {
                iv = ItemClass.GetItem(name, true);
                return iv != null && !iv.IsEmpty();
            }
            catch
            {
                return false;
            }
        }


        /// <summary>Count matching item type across bag + toolbelt.</summary>
        public static int CountItemType(EntityPlayerLocal p, int itemType)
        {
            if (p == null || itemType <= 0) return 0;
            int n = 0;
            try
            {
                if (p.bag != null)
                {
                    var slots = p.bag.GetSlots();
                    if (slots != null)
                        for (int i = 0; i < slots.Length; i++)
                            if (slots[i].itemValue.type == itemType) n += slots[i].count;
                }
                if (p.inventory != null)
                {
                    int sc = p.inventory.GetSlotCount();
                    for (int i = 0; i < sc; i++)
                    {
                        var s = p.inventory.GetItem(i);
                        if (s.itemValue.type == itemType) n += s.count;
                    }
                }
            }
            catch { /* */ }
            return n;
        }


        /// <summary>Put stack into bag first, else toolbelt. Returns true if accepted.</summary>
        public static bool TryGiveItem(EntityPlayerLocal p, ItemStack stack)
        {
            if (p == null || stack == null) return false;
            try
            {
                if (p.bag != null && p.bag.AddItem(stack)) return true;
            }
            catch { /* */ }
            try
            {
                if (p.inventory != null && p.inventory.AddItem(stack)) return true;
            }
            catch { /* */ }
            return false;
        }


        /// <summary>
        /// Free bag slots by dumping non-essential stacks via ItemDropServer.
        /// Combat loot floods CarryCapacity (45) before economy cases.
        /// </summary>
        public static int FreeBagSlots(EntityPlayerLocal p, int needFree)
        {
            if (p == null || needFree <= 0) return 0;
            int freed = 0;
            try
            {
                var bag = p.bag;
                var slots = bag?.GetSlots();
                if (slots == null) return 0;
                int total = slots.Length;
                int occ = 0;
                for (int i = 0; i < total; i++)
                    if (slots[i] != null && !slots[i].IsEmpty()) occ++;
                int free = total - occ;
                if (free >= needFree) return free;
                // Keep wood, food, coins, tools; dump the rest.
                for (int i = total - 1; i >= 0 && free < needFree; i--)
                {
                    var s = slots[i];
                    if (s == null || s.IsEmpty()) continue;
                    string n = "";
                    try { n = s.itemValue?.ItemClass?.GetItemName() ?? ""; } catch { /* */ }
                    bool keep = n.IndexOf("Wood", StringComparison.OrdinalIgnoreCase) >= 0
                        || n.IndexOf("food", StringComparison.OrdinalIgnoreCase) >= 0
                        || n.IndexOf("casinoCoin", StringComparison.OrdinalIgnoreCase) >= 0
                        || n.IndexOf("meleeTool", StringComparison.OrdinalIgnoreCase) >= 0
                        || n.IndexOf("StoneAxe", StringComparison.OrdinalIgnoreCase) >= 0;
                    if (keep) continue;
                    try
                    {
                        RequestItemDrop(p, s.Clone());
                        slots[i] = ItemStack.Empty.Clone();
                        free++;
                        freed++;
                    }
                    catch { /* */ }
                }
                try { bag.SetSlots(slots); } catch { /* */ }
            }
            catch { /* */ }
            return freed;
        }


        /// <summary>Equip first toolbelt slot holding itemType; returns slot or -1.</summary>
        public static int TryEquipItemType(EntityPlayerLocal p, int itemType)
        {
            if (p?.inventory == null || itemType <= 0) return -1;
            try
            {
                int sc = p.inventory.GetSlotCount();
                for (int i = 0; i < sc; i++)
                {
                    var s = p.inventory.GetItem(i);
                    if (s.itemValue.type != itemType || s.count <= 0) continue;
                    p.inventory.SetHoldingItemIdx(i);
                    return i;
                }
                // Move full bag stack into toolbelt so InstantAction DecHoldingItem can consume.
                if (p.bag != null)
                {
                    var slots = p.bag.GetSlots();
                    if (slots != null)
                    {
                        for (int i = 0; i < slots.Length; i++)
                        {
                            if (slots[i].itemValue.type != itemType || slots[i].count <= 0) continue;
                            int n = slots[i].count;
                            var move = new ItemStack(slots[i].itemValue.Clone(), n);
                            if (!p.inventory.AddItem(move)) continue;
                            try
                            {
                                slots[i] = ItemStack.Empty.Clone();
                                p.bag.SetSlots(slots);
                            }
                            catch { /* */ }
                            return TryEquipItemType(p, itemType);
                        }
                    }
                }
            }
            catch { /* */ }
            return -1;
        }


        /// <summary>
        /// Force full stack of itemType onto toolbelt slot 0 and hold it.
        /// Prefer this for InstantAction eat: bag-only stacks never hit DecHoldingItem.
        /// </summary>
        public static int EquipItemTypeFullStack(EntityPlayerLocal p, int itemType)
        {
            if (p?.inventory == null || itemType <= 0) return -1;
            try
            {
                // Gather total count + a clone of ItemValue from bag/toolbelt.
                ItemValue iv = null;
                int total = 0;
                if (p.bag != null)
                {
                    var slots = p.bag.GetSlots();
                    if (slots != null)
                    {
                        for (int i = 0; i < slots.Length; i++)
                        {
                            if (slots[i] == null || slots[i].IsEmpty()) continue;
                            if (slots[i].itemValue.type != itemType) continue;
                            if (iv == null) iv = slots[i].itemValue.Clone();
                            total += slots[i].count;
                            slots[i] = ItemStack.Empty.Clone();
                        }
                        try { p.bag.SetSlots(slots); } catch { /* */ }
                    }
                }
                int sc = p.inventory.GetSlotCount();
                for (int i = 0; i < sc; i++)
                {
                    var s = p.inventory.GetItem(i);
                    if (s == null || s.IsEmpty() || s.itemValue.type != itemType) continue;
                    if (iv == null) iv = s.itemValue.Clone();
                    total += s.count;
                    p.inventory.SetItem(i, ItemStack.Empty.Clone());
                }
                if (iv == null || total <= 0) return -1;
                // Prefer slot 0; overwrite empty or same-type; else first empty.
                int dst = -1;
                for (int i = 0; i < sc; i++)
                {
                    var s = p.inventory.GetItem(i);
                    if (s == null || s.IsEmpty()) { dst = i; break; }
                }
                if (dst < 0) dst = 0;
                p.inventory.SetItem(dst, new ItemStack(iv, total));
                p.inventory.SetHoldingItemIdx(dst);
                return dst;
            }
            catch { /* */ }
            return -1;
        }


        /// <summary>Client ItemDropServer → NetPackageItemDrop → server EntityItem.</summary>
        public static bool RequestItemDrop(EntityPlayerLocal p, ItemStack stack)
        {
            if (p == null || stack == null || GameManager.Instance == null) return false;
            try
            {
                var pos = p.GetPosition() + new Vector3(0.5f, 0.3f, 0.5f);
                GameManager.Instance.ItemDropServer(
                    stack, pos, Vector3.zero, p.entityId, 60f, false);
                return true;
            }
            catch
            {
                return false;
            }
        }


        /// <summary>
        /// Remove one unit of itemType from bag or toolbelt (local). Used when
        /// ItemActionEat InstantAction does not run under automation; C2S
        /// NetPackagePlayerInventory then carries the reduced stack to the server.
        /// </summary>
        public static bool TryConsumeOne(EntityPlayerLocal p, int itemType)
        {
            if (p == null || itemType <= 0) return false;
            try
            {
                if (p.inventory != null)
                {
                    int sc = p.inventory.GetSlotCount();
                    for (int i = 0; i < sc; i++)
                    {
                        var s = p.inventory.GetItem(i);
                        if (s == null || s.IsEmpty() || s.itemValue.type != itemType) continue;
                        if (s.count <= 1)
                            p.inventory.SetItem(i, ItemStack.Empty.Clone());
                        else
                        {
                            s.count -= 1;
                            p.inventory.SetItem(i, s);
                        }
                        return true;
                    }
                }
            }
            catch { /* */ }
            try
            {
                if (p.bag != null)
                {
                    var slots = p.bag.GetSlots();
                    if (slots != null)
                    {
                        for (int i = 0; i < slots.Length; i++)
                        {
                            var s = slots[i];
                            if (s == null || s.IsEmpty() || s.itemValue.type != itemType) continue;
                            if (s.count <= 1) slots[i] = ItemStack.Empty.Clone();
                            else { s.count -= 1; slots[i] = s; }
                            p.bag.SetSlots(slots);
                            return true;
                        }
                    }
                }
            }
            catch { /* */ }
            return false;
        }


        /// <summary>Resolve a named recipe (first match).</summary>
        public static Recipe FindRecipe(string itemName)
        {
            try
            {
                var r = CraftingManager.GetRecipe(itemName);
                if (r != null) return r;
            }
            catch { /* */ }
            try
            {
                var list = CraftingManager.GetRecipes(itemName);
                if (list != null && list.Count > 0) return list[0];
            }
            catch { /* */ }
            return null;
        }


        /// <summary>Open crafting and queue a recipe with short craft time. Returns false if UI missing.</summary>
        public static bool TryQueueCraft(EntityPlayerLocal player, Recipe recipe, float craftTimeSec, out string detail)
        {
            detail = "no xui";
            if (player == null || recipe == null) { detail = "null player/recipe"; return false; }
            try
            {
                if (!TryOpenWindow("crafting", out detail, requireOpen: false))
                    return false;
                var lp = LocalPlayerUI.GetUIForPrimaryPlayer();
                var xui = lp?.xui;
                if (xui == null) { detail = "no xui after open"; return false; }
                var wg = xui.FindWindowGroupByName("crafting") as XUiC_CraftingWindowGroup;
                if (wg == null)
                {
                    // Some builds use GetChildByType / controller attach after Open.
                    try { wg = xui.GetChildByType<XUiC_CraftingWindowGroup>(); } catch { /* */ }
                }
                if (wg == null) { detail = "no CraftingWindowGroup"; return false; }
                try { CraftingManager.UnlockRecipe(recipe, player); } catch { /* */ }
                bool ok = false;
                try { ok = wg.AddItemToQueue(recipe, 1, craftTimeSec); }
                catch
                {
                    try { ok = wg.AddItemToQueue(recipe, 1); }
                    catch (Exception ex) { detail = "queue ex " + ex.Message; return false; }
                }
                detail = "queued=" + ok + " recipe=" + recipe.GetName()
                    + " outType=" + recipe.itemValueType + " time=" + craftTimeSec.ToString("0.0");
                return ok;
            }
            catch (Exception ex)
            {
                detail = "craft ex " + ex.Message;
                return false;
            }
        }
    }
}
