==> head.txt <==
using System;
using UnityEngine;

namespace ZdtdPlaytest
{
    /// <summary>
    /// The Unity-side evidence behind <see cref="CaseDef.WalkEntity"/>: is the
    /// spawned entity actually standing on the traversable surface, and is it
    /// rendered with colliders a player can see and collide with.
    /// <para>Physics and mesh inspection, not case construction, so it lives
    /// here rather than in the provider-facing <see cref="CaseDef"/> contract
    /// (CaseDef.cs), which external providers build cases from.</para>
    /// </summary>
    internal static class EntityProbe
    {

==> body.cs <==
    static bool ReportWalkEntityRenderProbe(string id, EntityAlive alive, float elapsed)
    {
        if (alive == null)
        {
            Report.Info(id + ": render-probe entity=<null>");
            return false;
        }
        var meshes = alive.GetComponentsInChildren<SkinnedMeshRenderer>(true);
        if (meshes == null || meshes.Length == 0)
        {
            Report.Info(id + ": render-probe smr=<none>");
            return false;
        }
        var colliders = alive.GetComponentsInChildren<Collider>(true);
        int activeColliders = 0;
        int activeSolidColliders = 0;
        string collisionRay = "no-active-solid-collider";
        bool collisionHit = false;
        string physicsCapsule = "<none>";
        for (int i = 0; colliders != null && i < colliders.Length; i++)
        {
            var collider = colliders[i];
            var capsule = collider as CapsuleCollider;
            if (capsule != null && collider != null && collider.name == "Physics")
            {
                physicsCapsule = "center=" + capsule.center.ToString("F2")
                    + " radius=" + capsule.radius.ToString("0.000")
                    + " height=" + capsule.height.ToString("0.000")
                    + " bottom=" + (capsule.center.y - capsule.height * 0.5f).ToString("0.000")
                    + " enabled=" + capsule.enabled
                    + " active=" + capsule.gameObject.activeInHierarchy;
            }
            if (collider == null || !collider.enabled || !collider.gameObject.activeInHierarchy)
                continue;
            activeColliders++;
            if (!collider.isTrigger) activeSolidColliders++;
            if (!collisionHit && !collider.isTrigger)
            {
                RaycastHit colliderHit;
                var ext = collider.bounds.extents;
                float reach = Mathf.Max(ext.x, ext.y, ext.z) + 0.35f;
                var origin = collider.bounds.center + Vector3.up * reach;
                if (Physics.Raycast(origin, Vector3.down, out colliderHit, reach * 2f))
                {
                    bool target = colliderHit.transform != null
                        && colliderHit.transform.IsChildOf(alive.transform);
                    collisionRay = (colliderHit.transform != null
                        ? colliderHit.transform.name : "<null>")
                        + "@" + colliderHit.distance.ToString("0.00") + " target=" + target;
                    if (target) collisionHit = true;
                }
            }
        }
        bool collisionReady = activeSolidColliders > 0 && collisionHit
            && physicsCapsule != "<none>";
        Bounds renderedBounds;
        bool hasRenderedBounds = Helpers.TryGetRenderedBounds(
            alive.gameObject, out renderedBounds);
        float terrainTop = float.NaN;
        float surfaceRay = float.NaN;
        string surfaceHit = "<not-run>";
        float visualBottom = float.NaN;
        float groundClearance = float.NaN;
        bool groundReady = false;
        try
        {
            var world = GameManager.Instance != null ? GameManager.Instance.World : null;
            var absolute = alive.GetPosition();
            if (world != null && hasRenderedBounds)
            {
                var column = Helpers.BlockColumn(absolute);
                terrainTop = world.GetHeight(column.x, column.z) + 1f;
                TryGroundSurface(
                    alive, absolute.x, absolute.y, absolute.z,
                    out surfaceRay, out surfaceHit);
                visualBottom = renderedBounds.min.y + Origin.position.y;
                float measuredSurface = float.IsNaN(surfaceRay) ? terrainTop : surfaceRay;
                groundClearance = visualBottom - measuredSurface;
                groundReady = !float.IsNaN(surfaceRay)
                    && groundClearance >= -0.08f && groundClearance <= 0.20f;
            }
        }
        catch { }
        Vector3 cameraPos, cameraForward;
        bool hasCamera = Helpers.TryGetCaptureCameraPose(out cameraPos, out cameraForward);
        for (int i = 0; i < meshes.Length; i++)
        {
            var smr = meshes[i];
            var material = smr != null ? smr.sharedMaterial : null;
            var shader = material != null ? material.shader : null;
            bool setPass = false;
            string setPassError = "none";
            if (material != null)
            {
                try { setPass = material.SetPass(0); }
                catch (Exception ex) { setPassError = ex.GetType().Name; }
            }

            string baked = "n/a";
            Mesh bakedMesh = null;
            try
            {
                bakedMesh = new Mesh();
                smr.BakeMesh(bakedMesh);
                baked = bakedMesh.bounds.ToString("F2") + " v=" + bakedMesh.vertexCount;
            }
            catch (Exception ex) { baked = "error:" + ex.GetType().Name; }
            finally
            {
                if (bakedMesh != null) UnityEngine.Object.Destroy(bakedMesh);
            }

            string camera = "<none>";
            if (hasCamera)
            {
                var toMesh = smr.bounds.center - cameraPos;
                float distance = toMesh.magnitude;
                float facing = distance > 0.001f
                    ? Vector3.Dot(cameraForward.normalized, toMesh / distance) : 1f;
                string ray = "clear";
                RaycastHit hit;
                if (distance > 0.001f && Physics.Raycast(cameraPos, toMesh / distance, out hit, distance + 0.1f))
                {
                    bool target = hit.transform != null && hit.transform.IsChildOf(alive.transform);
                    ray = (hit.transform != null ? hit.transform.name : "<null>")
                        + "@" + hit.distance.ToString("0.00") + " target=" + target;
                }
                camera = "pos=" + cameraPos.ToString("F2")
                    + " forward=" + cameraForward.ToString("F2")
                    + " distance=" + distance.ToString("0.00")
                    + " facing=" + facing.ToString("0.000")
                    + " ray=" + ray;
            }

            Report.Info(id + ": render-probe t=" + elapsed.ToString("0.00")
                + " smr=" + (smr != null ? smr.name : "<null>")
                + " enabled=" + (smr != null && smr.enabled)
                + " mesh=" + (smr != null && smr.sharedMesh != null ? smr.sharedMesh.name : "<null>")
                + " meshBounds=" + (smr != null && smr.sharedMesh != null ? smr.sharedMesh.bounds.ToString("F2") : "n/a")
                + " worldBounds=" + (smr != null ? smr.bounds.ToString("F2") : "n/a")
                + " bakedBounds=" + baked
                + " material=" + (material != null ? material.name : "<null>")
                + " shader=" + (shader != null ? shader.name : "<null>")
                + " supported=" + (shader != null ? shader.isSupported.ToString() : "n/a")
                + " passes=" + (shader != null ? shader.passCount.ToString() : "n/a")
                + " SetPass0=" + setPass
                + " SetPassError=" + setPassError
                + " colliders=" + (colliders != null ? colliders.Length : 0)
                + " active=" + activeColliders
                + " solid=" + activeSolidColliders
                + " PhysicsCapsule=" + physicsCapsule
                + " collisionRay=" + collisionRay
                + " collisionReady=" + collisionReady
                + " voxelTop=" + terrainTop.ToString("0.000")
                + " surfaceRay=" + surfaceRay.ToString("0.000")
                + " surfaceHit=" + surfaceHit
                + " voxelMinusSurface=" + (terrainTop - surfaceRay).ToString("0.000")
                + " visualBottom=" + visualBottom.ToString("0.000")
                + " groundClearance=" + groundClearance.ToString("0.000")
                + " groundReady=" + groundReady
                + " camera=" + camera);
        }
        return collisionReady && groundReady;
    }

    /// <summary>The root Y that puts a spawned entity's feet on the terrain at (x, z).
    /// <para>The engine grounds an entity by the CharacterController capsule
    /// the generated model carries on its `Physics` child node; the capsule's
    /// bottom is `center.y - height/2` below that node, and a generated
    /// creature authors it so the bottom sits at the mesh's feet (a hair
    /// below the root). A downward physics ray gives the actual traversable
    /// surface of slopes and partial blocks; `World.GetHeight(x,z) + 1` is
    /// only the full-voxel fallback. Subtracting the capsule bottom from
    /// that surface puts the capsule, and therefore the authored feet, on
    /// it.</para></summary>
    static float GroundYFor(World world, EntityAlive alive, float x, float z)
    {
        // GetHeightAt is the terrain generator's uncarved heightmap. It
        // measured world Y 60.05 in a live column whose top voxel face was
        // Y 61, so the old harness forced a healthy creature nearly one
        // full block into the road every tick. GetHeight returns the loaded
        // top block; +1 is its standing surface.
        var column = Helpers.BlockColumn(new Vector3(x, alive.GetPosition().y, z));
        float voxelTop = world.GetHeight(column.x, column.z) + 1f;
        float surface;
        string surfaceHit;
        if (!TryGroundSurface(alive, x, alive.GetPosition().y, z, out surface, out surfaceHit))
            surface = voxelTop;
        float capsuleBottom = 0f;
        var colliders = alive != null
            ? alive.GetComponentsInChildren<CapsuleCollider>(true) : null;
        for (int i = 0; colliders != null && i < colliders.Length; i++)
        {
            var capsule = colliders[i];
            if (capsule != null && capsule.name == "Physics")
            {
                capsuleBottom = capsule.center.y - capsule.height * 0.5f;
                break;
            }
        }
        return surface - capsuleBottom + 0.01f;
    }

    static bool TryGroundSurface(
        EntityAlive alive,
        float worldX,
        float worldY,
        float worldZ,
        out float surfaceWorldY,
        out string hitName)
    {
        surfaceWorldY = float.NaN;
        hitName = "<none>";
        // Physics and render transforms use rebased coordinates. Cast from
        // well above the candidate column, then convert the hit back to
        // absolute world Y. RaycastAll lets the entity ignore its own
        // bone/capsule colliders when the next orbit sample overlaps it.
        var origin = new Vector3(
            worldX - Origin.position.x,
            worldY - Origin.position.y + 10f,
            worldZ - Origin.position.z);
        RaycastHit[] hits;
        try { hits = Physics.RaycastAll(origin, Vector3.down, 200f, 268500992); }
        catch { return false; }
        if (hits == null || hits.Length == 0) return false;
        Array.Sort(hits, (left, right) => left.distance.CompareTo(right.distance));
        for (int i = 0; i < hits.Length; i++)
        {
            var transform = hits[i].transform;
            if (transform != null && alive != null && transform.IsChildOf(alive.transform))
                continue;
            surfaceWorldY = hits[i].point.y + Origin.position.y;
            hitName = transform != null ? transform.name : "<null>";
            return true;
        }
        return false;
    }
    }
}
