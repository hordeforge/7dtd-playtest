using System;
using System.Collections;
using System.Collections.Generic;
using System.Globalization;
using System.Reflection;
using UnityEngine;

namespace ZdtdPlaytest
{
    /// <summary>Skinned-rig inspection reports: bone names, bounds, graft state, and bind poses for garment authoring evidence.</summary>
    public static partial class Helpers
    {

        /// <summary>
        /// Every bone name the wearer's skinned renderers are bound to,
        /// distinct and sorted.
        ///
        /// <para>This is the one input a skinned garment cannot be authored
        /// without. SDCS rebinds a gear prefab's <c>SkinnedMeshRenderer.bones</c>
        /// to the wearer <b>by name</b> through a string-keyed
        /// <c>TransformCatalog</c>, and a name that does not match becomes a
        /// null bone with no error raised, so the exact spelling of every bone
        /// decides whether a garment deforms or hangs in space. The names live
        /// in the game's own asset bundles, not in XML or IL, so the only place
        /// to read them is off a real wearer in a running client.</para>
        ///
        /// <para>Sorted because the answer is reference material to copy into
        /// an authoring doc, and a set that reorders between runs is not a
        /// reference. Pass any <see cref="EntityAlive"/>: the local player is
        /// the usual subject, but a spawned entity works the same way.</para>
        /// </summary>
        public static List<string> RigBoneNames(EntityAlive entity)
        {
            var names = new List<string>();
            if (entity == null) return names;
            try
            {
                var renderers = entity.GetComponentsInChildren<SkinnedMeshRenderer>(true);
                if (renderers == null) return names;
                var seen = new HashSet<string>();
                for (int i = 0; i < renderers.Length; i++)
                {
                    var bones = renderers[i] != null ? renderers[i].bones : null;
                    if (bones == null) continue;
                    for (int b = 0; b < bones.Length; b++)
                    {
                        // A null bone here is exactly the failure this exists to
                        // prevent downstream, so it is skipped rather than named.
                        if (bones[b] != null && seen.Add(bones[b].name)) names.Add(bones[b].name);
                    }
                }
            }
            catch { /* a partial rig is still worth reporting */ }
            names.Sort(StringComparer.Ordinal);
            return names;
        }


        /// <summary>
        /// A grafted garment's renderer as the engine left it: mesh, vertex
        /// count, bind poses, bone count, null-bone count, root bone and local
        /// bounds — one line per matching renderer, sorted.
        ///
        /// <para>This is step three of diagnosing gear that does not appear,
        /// and the step that separates the two failures which look identical
        /// from a frame. SDCS rebinds a gear prefab's bones to the wearer
        /// <b>by name</b>, and a name the wearer does not have becomes a null
        /// bone with <b>no error raised</b>: the garment loads, the renderer
        /// reports a sensible bounds, and the piece deforms wrongly or hangs in
        /// space. Nulls mean a name mismatch. Correct names with a piece that
        /// does not move is the missing-<c>Origin</c> fault instead, and
        /// collapsed bounds is neither.</para>
        ///
        /// <para>Only then dimensions. Reaching for dimensions first is what
        /// this ordering exists to prevent, and it has cost several sessions of
        /// widening meshes that were never the problem.</para>
        /// </summary>
        public static List<string> GraftReport(EntityAlive entity, string prefix)
        {
            var lines = new List<string>();
            if (entity == null || string.IsNullOrEmpty(prefix)) return lines;
            try
            {
                var renderers = entity.GetComponentsInChildren<SkinnedMeshRenderer>(true);
                if (renderers == null) return lines;
                for (int i = 0; i < renderers.Length; i++)
                {
                    var renderer = renderers[i];
                    if (renderer == null || renderer.sharedMesh == null) continue;
                    string mesh = renderer.sharedMesh.name;
                    if (string.IsNullOrEmpty(mesh) || !mesh.StartsWith(prefix)) continue;

                    var bones = renderer.bones;
                    int nulls = 0;
                    var names = new List<string>();
                    for (int b = 0; bones != null && b < bones.Length; b++)
                    {
                        if (bones[b] == null) { nulls++; names.Add("<null>"); }
                        else names.Add(bones[b].name);
                    }
                    var root = renderer.rootBone;
                    var local = renderer.localBounds;
                    lines.Add("mesh=" + mesh
                        + " verts=" + renderer.sharedMesh.vertexCount
                        + " bindposes=" + renderer.sharedMesh.bindposes.Length
                        + " bones=" + (bones == null ? 0 : bones.Length)
                        + " nulls=" + nulls
                        + " root=" + (root == null ? "<null>" : root.name)
                        + " center=" + local.center.ToString("0.###")
                        + " extents=" + local.extents.ToString("0.###")
                        + " boneNames=" + string.Join(",", names.ToArray()));
                }
            }
            catch { /* a partial report still names the failure shape */ }
            lines.Sort(StringComparer.Ordinal);
            return lines;
        }


        /// <summary>
        /// Every skinned renderer on a wearer as
        /// <c>name center=(x,y,z) extents=(x,y,z)</c>, sorted.
        ///
        /// <para>The local bounds of the wearer's own meshes, which is what a
        /// garment has to clear. Authoring one against the bind pose alone
        /// fits the *skeleton* rather than the body wrapped around it, and the
        /// difference shows up as skin through the shoulders, the chest and
        /// the face.</para>
        ///
        /// <para>Read the numbers with care: this is a per-renderer local AABB,
        /// so a whole-body renderer's box is bounded by whatever sticks out
        /// furthest — in an A-pose that is the toes and the hands, not the
        /// chest. Reading such a box as a chest measurement is a mistake that
        /// has already been made twice, in both directions.</para>
        /// </summary>
        public static List<string> RigBounds(EntityAlive entity)
        {
            var lines = new List<string>();
            if (entity == null) return lines;
            try
            {
                var renderers = entity.GetComponentsInChildren<SkinnedMeshRenderer>(true);
                if (renderers == null) return lines;
                for (int i = 0; i < renderers.Length; i++)
                {
                    var renderer = renderers[i];
                    if (renderer == null || renderer.sharedMesh == null) continue;
                    var bounds = renderer.localBounds;
                    lines.Add(renderer.gameObject.name
                        + " center=" + bounds.center.ToString("0.###")
                        + " extents=" + bounds.extents.ToString("0.###"));
                }
            }
            catch { /* a partial list is still a measurement */ }
            lines.Sort(StringComparer.Ordinal);
            return lines;
        }


        /// <summary>
        /// The mod-authored skinned meshes actually grafted on a wearer, as
        /// <c>name=vertexCount</c>, sorted.
        ///
        /// <para><b>This is how a mod proves its gear reached the client at
        /// all.</b> Everything else a suite can assert about worn armor is
        /// satisfied without it: the item sits in an equipment slot whether or
        /// not its prefab loaded, and the wearer has a rig whether or not
        /// anything was grafted onto it. So a suite can go green, stage a
        /// scene, and photograph a wearer wearing nothing this mod
        /// built — and every frame from that run is then evidence about the
        /// wrong thing.</para>
        ///
        /// <para>That is not hypothetical. It cost a full afternoon on
        /// 2026-08-24: four green runs, four sets of frames, several rounds of
        /// geometry "fixes" judged against pictures, and no line anywhere in
        /// the preserved output that said whether the mod's meshes were
        /// present. The fix is an assertion a stale bundle cannot pass.</para>
        ///
        /// <para><paramref name="prefix"/> is matched against
        /// <c>sharedMesh.name</c>, not against the part transform's name. SDCS
        /// parts are called <c>body</c>, <c>head</c>, <c>hands</c> and
        /// <c>feet</c> — the same names the base body uses — so matching a
        /// transform proves nothing. A mesh name belongs to whoever authored
        /// the asset, so a mod's own prefix cannot collide with a vanilla
        /// one.</para>
        ///
        /// <para>Sorted, and the vertex count included, because the caller's
        /// real question is usually not "is anything there" but "is what is
        /// there what I just built". A count that disagrees with the
        /// generator's own build log is a stale bundle, and that is the single
        /// most common way this fails.</para>
        /// </summary>
        public static List<string> GraftedMeshes(EntityAlive entity, string prefix)
        {
            var found = new List<string>();
            if (entity == null || string.IsNullOrEmpty(prefix)) return found;
            try
            {
                var renderers = entity.GetComponentsInChildren<SkinnedMeshRenderer>(true);
                if (renderers == null) return found;
                for (int i = 0; i < renderers.Length; i++)
                {
                    var renderer = renderers[i];
                    if (renderer == null || renderer.sharedMesh == null) continue;
                    string mesh = renderer.sharedMesh.name;
                    if (string.IsNullOrEmpty(mesh) || !mesh.StartsWith(prefix)) continue;
                    found.Add(mesh + "=" + renderer.sharedMesh.vertexCount);
                }
            }
            catch { /* a partial answer still distinguishes present from absent */ }
            found.Sort(StringComparer.Ordinal);
            return found;
        }


        /// <summary>
        /// The wearer's rig as authoring reference: one line per bone, with its
        /// parent, its local position, and its <b>bind pose</b>.
        ///
        /// <para><see cref="RigBoneNames"/> gives the names a garment must bind
        /// to, and names alone cannot author one. A skinned mesh carries a bind
        /// pose, so an armature whose joints sit somewhere else deforms wrongly
        /// even when every name matches.</para>
        ///
        /// <para>The bind pose comes from <c>sharedMesh.bindposes</c>, not from
        /// the live transform. An earlier version of this method reported
        /// <c>localRotation</c> and called it a rest transform: that is the
        /// <i>animated</i> pose, whatever frame the wearer happened to be on,
        /// and it changes between two runs of the same suite. Positions survive
        /// that mistake because bone lengths do not animate; rotations do not.
        /// The bind pose is the matrix the mesh was actually skinned against,
        /// which is what an armature has to reproduce.</para>
        ///
        /// <para>Ordered by name: this gets copied into a document, and a
        /// report that reorders between runs cannot be diffed against the next
        /// game build.</para>
        ///
        /// <para>Format, one bone per line:
        /// <c>name|parent|localPos x,y,z|bindPos x,y,z|bindRot x,y,z,w</c>.
        /// The bind columns are the inverse of the bind-pose matrix, i.e. the
        /// bone's own transform at the moment the mesh was skinned. A bone with
        /// no bind pose (it belongs to no renderer's mesh) reports empty bind
        /// columns rather than being dropped.</para>
        /// </summary>
        public static List<string> RigPoseReport(EntityAlive entity)
        {
            var lines = new List<string>();
            if (entity == null) return lines;
            try
            {
                var renderers = entity.GetComponentsInChildren<SkinnedMeshRenderer>(true);
                if (renderers == null) return lines;
                var seen = new HashSet<string>();
                for (int i = 0; i < renderers.Length; i++)
                {
                    var smr = renderers[i];
                    var bones = smr != null ? smr.bones : null;
                    if (bones == null) continue;
                    var mesh = smr.sharedMesh;
                    var binds = mesh != null ? mesh.bindposes : null;
                    for (int b = 0; b < bones.Length; b++)
                    {
                        var bone = bones[b];
                        if (bone == null || !seen.Add(bone.name)) continue;

                        string bindPos = "";
                        string bindRot = "";
                        if (binds != null && b < binds.Length)
                        {
                            // bindposes holds the world-to-bone matrix, so the
                            // bone's own bind transform is its inverse.
                            var m = binds[b].inverse;
                            bindPos = V3(m.GetColumn(3));
                            var q = m.rotation;
                            bindRot = F(q.x) + "," + F(q.y) + "," + F(q.z) + "," + F(q.w);
                        }

                        lines.Add(bone.name + "|"
                            + (bone.parent != null ? bone.parent.name : "") + "|"
                            + V3(bone.localPosition) + "|" + bindPos + "|" + bindRot);
                    }
                }
            }
            catch { /* a partial rig is still worth reporting */ }
            lines.Sort(StringComparer.Ordinal);
            return lines;
        }


        static string F(float v)
        {
            return v.ToString("0.######", CultureInfo.InvariantCulture);
        }


        static string V3(Vector3 v)
        {
            return F(v.x) + "," + F(v.y) + "," + F(v.z);
        }


        static string V3(Vector4 v)
        {
            return F(v.x) + "," + F(v.y) + "," + F(v.z);
        }
    }
}
