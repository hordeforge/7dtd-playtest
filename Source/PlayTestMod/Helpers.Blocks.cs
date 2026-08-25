using System;
using UnityEngine;

namespace ZdtdPlaytest
{
    /// <summary>
    /// Block placement and block-entity model helpers, generalized from the
    /// 7dtd-asset-pipeline SelfTestMod block acceptance (model suite and place
    /// suite). Measured facts behind them:
    ///
    /// - a placed block's model is not spawned by the placement itself: the
    ///   chunk instantiates it in a deferred display pass
    ///   (Chunk.OnDisplayBlockEntities -&gt; GameObjectPool) that walks its
    ///   block-entity stubs with a per-call budget, so a freshly placed stub
    ///   at the end of a long list can take seconds to reach. The stub
    ///   (BlockEntityData) exists immediately; its transform appears only
    ///   when the pass reaches it.
    /// - the pass may leave the model's renderers disabled (collision-only
    ///   mesh path), so a render check must switch them on.
    /// - a ModelEntity block placed without support under every voxel it
    ///   occupies becomes a falling entity in the server's stability pass and
    ///   the client just reports air; the placement spot must be grounded.
    /// - the debug console swallows input-driven item actions while open, so
    ///   a placement fired from a case must close it first.
    /// </summary>
    public static partial class Helpers
    {
        /// <summary>
        /// The block entity data (stub) at a world position, or null. The stub
        /// exists as soon as the block is placed; its <c>transform</c> appears
        /// when the chunk's display pass reaches it.
        /// </summary>
        public static BlockEntityData BlockEntityDataAt(World world, Vector3i pos)
        {
            try
            {
                var chunk = world.ChunkCache.GetChunkFromWorldPos(pos);
                return chunk != null ? chunk.GetBlockEntity(pos) : null;
            }
            catch
            {
                return null;
            }
        }

        /// <summary>
        /// Switch a block entity's model on: the chunk display pass can leave
        /// the renderers disabled (collision-only mesh path) and the model
        /// GameObject inactive. Returns false when the entity has no
        /// transform yet (the display pass has not reached the stub).
        /// </summary>
        public static bool ActivateBlockEntityModel(BlockEntityData bed)
        {
            if (bed == null || bed.transform == null) return false;
            var renderers = bed.transform.GetComponentsInChildren<Renderer>(true);
            if (renderers == null || renderers.Length == 0) return false;
            foreach (var r in renderers)
            {
                r.enabled = true;
            }
            if (!bed.transform.gameObject.activeInHierarchy)
            {
                bed.transform.gameObject.SetActive(true);
            }
            return true;
        }

        /// <summary>
        /// A grounded air voxel ahead of the camera: surface height at the
        /// target column (the terrain a few blocks ahead may sit higher or
        /// lower than the player's feet), one above it, with a solid voxel
        /// below so the server's stability pass does not turn the placement
        /// into a falling block. Returns null when no candidate qualifies.
        /// </summary>
        public static Vector3i? FindGroundedAir(World world, Vector3i feet, Vector3 ahead, int distance = 2)
        {
            var dx = Mathf.RoundToInt(ahead.x * distance);
            var dz = Mathf.RoundToInt(ahead.z * distance);
            if (dx == 0 && dz == 0)
            {
                dx = Mathf.RoundToInt(ahead.x * (distance + 1));
                dz = Mathf.RoundToInt(ahead.z * (distance + 1));
            }
            var tx = feet.x + dx;
            var tz = feet.z + dz;
            int surface = feet.y;
            try
            {
                surface = Mathf.RoundToInt(world.GetHeightAt(tx, tz));
            }
            catch
            {
                // keep the player's surface; the support check below still applies
            }
            var candidates = new[]
            {
                new Vector3i(tx, surface + 1, tz),
                new Vector3i(tx, surface + 2, tz),
                new Vector3i(tx, feet.y + 1, tz),
                new Vector3i(feet.x + dx, surface + 1, feet.z + dz),
            };
            foreach (var at in candidates)
            {
                if (world.GetBlock(at).type != 0)
                {
                    continue; // occupied
                }
                if (world.GetBlock(at + Vector3i.down).isair)
                {
                    continue; // no support: the stability pass would drop it
                }
                return at;
            }
            return null;
        }

        /// <summary>
        /// Point the local player's placement at a voxel: fills the player's
        /// HitInfo the way a raycast hitting the floor below the voxel would,
        /// so ItemActionPlaceAsBlock.ExecuteAction places the block there.
        /// lastBlockPos is the air voxel the block goes into; hit.pos is the
        /// point on the floor the ray struck. Returns false when the voxel or
        /// its support is missing.
        /// </summary>
        public static bool AimBlockPlacement(EntityPlayerLocal player, World world, Vector3i at)
        {
            if (player == null || world == null) return false;
            if (world.GetBlock(at).type != 0) return false; // occupied
            if (world.GetBlock(at + Vector3i.down).isair) return false; // no support
            var hit = player.HitInfo;
            hit.bHitValid = true;
            hit.tag = "";
            hit.lastBlockPos = at;
            hit.hit.blockPos = at;
            hit.hit.pos = new Vector3(at.x + 0.5f, at.y - 0.5f, at.z + 0.5f);
            hit.hit.blockFace = BlockFace.Top;
            hit.hit.distanceSq = 9f;
            try
            {
                hit.hit.voxelData = HitInfoDetails.VoxelData.GetFrom(world, at + Vector3i.down);
            }
            catch
            {
                hit.hit.voxelData = default;
            }
            return true;
        }

        /// <summary>
        /// Close the debug console when it is open; it swallows input-driven
        /// item actions otherwise. No-op when the console does not exist.
        /// </summary>
        public static void CloseDebugConsole()
        {
            try
            {
                if (GUIWindowConsole.instance != null)
                {
                    GUIWindowConsole.instance.CloseConsole();
                }
            }
            catch
            {
                // console may not exist in this game state; the caller's own
                // checks still decide
            }
        }
    }
}
