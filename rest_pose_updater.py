bl_info = {
    "name": "Rest Pose Updater (Preserve Mesh Weights & Actions!!)",
    "author": "nebobyeoli",
    "version": (1, 10),
    "blender": (4, 5, 0),
    "location": "View3D > Sidebar (N) > Rest Pose Updater Tab",
    "description": "Auto-detects modified pose bones, mirrors them for symmetric bones, then does Apply Pose to Rest Pose, preserving mesh weights, mesh mirror seams, and animation Actions.",
    "doc_url": "https://github.com/nebobyeoli/blender-rest-pose-updater", # "Website"
    "tracker_url": "", # "Feedback - Report a Bug"
    "category": "Animation",
}

import bpy
from mathutils import Quaternion, Vector

def get_symmetrical_bone_name(name):
    suffixes = [(".L", ".R"), ("_L", "_R"), (".l", ".r"), ("_l", "_r"), ("Left", "Right"), ("left", "right")]
    for left, right in suffixes:
        if name.endswith(left):
            return name[:-len(left)] + right
        if name.endswith(right):
            return name[:-len(right)] + left
    return None

class POSE_OT_auto_rest_pose_sync(bpy.types.Operator):
    """Auto-detect bones which transforms have changed from the Rest pose,\n\napply the change to all weighted meshes,\n\napply the inverse to all Action tracks, preserving hierarchy/IK bones"""
    bl_idname = "pose.auto_rest_pose_sync"
    bl_label = "Sync Rest Pose & Actions"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        obj = context.active_object
        if not obj:
            self.report({'ERROR'}, "Nothing selected. Select an Armature and try again")
            return {'CANCELLED'}
        if obj.type != 'ARMATURE':
            self.report({'ERROR'}, f"Selected/Active object \"{obj.name}\" is not an Armature.")
            return {'CANCELLED'}


        self.report({'INFO'}, f"Selected/Active Armature: \"{obj.name}\"")

        # Ensure script operations run in Pose Mode internally
        original_mode = context.mode
        self.report({'INFO'}, f"Current mode: ( {original_mode} )")
        if original_mode != 'POSE':
            self.report({'INFO'}, "Changing mode to Pose mode to detect transform-changed bones.")
            bpy.ops.object.mode_set(mode='POSE')

        # --- STEP 1: Auto-Detect Modified Bones ---
        modified_bones = set()
        identity_quat = Quaternion((1, 0, 0, 0))
        
        for pb in obj.pose.bones:
            has_moved = pb.location.length > 0.0001
            
            has_rotated = False
            if pb.rotation_mode == 'QUATERNION':
                quat_diff = pb.rotation_quaternion.rotation_difference(identity_quat)
                has_rotated = abs(quat_diff.angle) > 0.0001
            elif pb.rotation_mode not in ('QUATERNION', 'AXIS_ANGLE'):
                has_rotated = Vector(pb.rotation_euler).length > 0.0001
                
            if has_moved or has_rotated:
                modified_bones.add(pb)

        if not modified_bones:
            self.report({'INFO'}, "No modified bones detected in current pose.")
            if context.mode != original_mode:
                self.report({'INFO'}, f"Changing mode back to original mode ( {original_mode} ).")
                bpy.ops.object.mode_set(mode=original_mode)
            return {'FINISHED'}

        detected_names = ", ".join([b.name for b in modified_bones])
        self.report({'INFO'}, f"Bones Changed: {detected_names}")

        # --- STEP 2: Sync Symmetrical Counterparts (Keep Larger Transform Side & Clear Smaller) ---
        bones_to_process = set()
        bones_to_mirror = []
        handled_pairs = set()

        for bone in modified_bones:
            sym_name = get_symmetrical_bone_name(bone.name)
            sym_bone = obj.pose.bones.get(sym_name) if sym_name else None

            # Skip if this pair was already handled from the other side
            pair_key = tuple(sorted([bone.name, sym_name])) if sym_name else (bone.name,)
            if pair_key in handled_pairs:
                continue
            handled_pairs.add(pair_key)

            if sym_bone and sym_bone in modified_bones:
                # Calculate transform magnitude for the current bone
                loc_mag = bone.location.length
                quat_diff = bone.rotation_quaternion.rotation_difference(identity_quat)
                rot_mag = abs(quat_diff.angle)
                bone_total_delta = loc_mag + rot_mag

                # Calculate transform magnitude for its symmetrical partner
                sym_loc_mag = sym_bone.location.length
                sym_quat_diff = sym_bone.rotation_quaternion.rotation_difference(identity_quat)
                sym_rot_mag = abs(sym_quat_diff.angle)
                sym_total_delta = sym_loc_mag + sym_rot_mag

                # Compare, clear the smaller transform side completely, and queue the mirror operation
                if bone_total_delta >= sym_total_delta:
                    bones_to_process.add(bone)
                    bones_to_process.add(sym_bone)
                    bones_to_mirror.append(bone)
                    
                    # Clear the smaller side so the paste-flip operation has a clean target canvas
                    sym_bone.location = Vector((0, 0, 0))
                    sym_bone.rotation_quaternion = Quaternion((1, 0, 0, 0))
                else:
                    bones_to_process.add(sym_bone)
                    bones_to_process.add(bone)
                    bones_to_mirror.append(sym_bone)
                    
                    # Clear the smaller side completely
                    bone.location = Vector((0, 0, 0))
                    bone.rotation_quaternion = Quaternion((1, 0, 0, 0))
            else:
                # Standard behavior if the partner isn't modified
                bones_to_process.add(bone)
                if sym_bone:
                    bones_to_process.add(sym_bone)
                    bones_to_mirror.append(bone)


        if bones_to_mirror:
            self.report({'INFO'}, f"Syncing {len(bones_to_mirror)} bone pairs via paste-flipped...")
            bpy.ops.pose.select_all(action='DESELECT')
            for bone in bones_to_mirror:
                bone.bone.select = True
            
            bpy.ops.pose.copy()
            bpy.ops.pose.paste(flipped=True)
            context.view_layer.update()

        # --- STEP 3: Cache Transformation Data ---
        bone_transforms = {}
        for pose_bone in bones_to_process:
            if pose_bone.rotation_mode != 'QUATERNION':
                self.report({'INFO'}, f"Warning: Skipping '{pose_bone.name}' (Not using Quaternion mode).")
                continue
                
            bone_transforms[pose_bone.name] = {
                'inverse_quat': pose_bone.rotation_quaternion.copy().inverted(),
                'current_loc': pose_bone.location.copy(),
                'rot_path': f'pose.bones["{pose_bone.name}"].rotation_quaternion',
                'loc_path': f'pose.bones["{pose_bone.name}"].location'
            }

        # --- STEP 4: Map Center Seam Vertices & Freeze Shape via Duplicate ---
        connected_meshes = [m for m in bpy.data.objects if m.type == 'MESH' and any(mod.type == 'ARMATURE' and mod.object == obj for mod in m.modifiers)]
        
        mesh_pairings = {}
        center_vertex_masks = {}
        linked_meshes = []  # Tracks inter-scene linked instances to reconstruct later
        
        self.report({'INFO'}, "Changing mode to Object mode to cache mesh center vertices, to prevent seams on mesh with mirror modifier after operation.")
        bpy.ops.object.mode_set(mode='OBJECT')
        
        for mesh_obj in connected_meshes:

            should_bypass = False
            bypass_linkedObjs = context.scene.sync_handle_linked_meshes
            bypass_dupLinkedObjs = context.scene.sync_handle_duplicate_linked

            if bypass_linkedObjs:
                # Check if the mesh is linked from another external file library OR linked across internal scenes
                is_external_link = (mesh_obj.library is not None or mesh_obj.data.library is not None)
                # is_scene_link = (hasattr(mesh_obj, "users_scene") and (mesh_obj.users_scene > 1))

                # Iterates through all internal scenes inside your current file block. If the mesh object exists in any scene collections other than the active window, it is a cross-scene link.
                linked_scenes = [ sce.name for sce in bpy.data.scenes if mesh_obj.name in sce.collection.objects or mesh_obj.name in sce.objects ] # cf. sum(1 for sce in ... )
                is_scene_link = len(linked_scenes) > 1

                if is_external_link:
                    self.report({'INFO'}, f"\"{mesh_obj.name}\": Externally-linked object (library: {mesh_obj.library}, data.library: {mesh_obj.data.library})")
                if is_scene_link:
                    self.report({'INFO'}, f"\"{mesh_obj.name}\": Scene-linked object ({linked_scenes})")

                if is_external_link or is_scene_link:
                    should_bypass = True
                
            if not should_bypass and bypass_dupLinkedObjs:
                # Detect Duplicate Linked (Alt + D) structures by checking mesh block instance usage
                is_dupLinked = mesh_obj.data and hasattr(mesh_obj.data, "users") and (mesh_obj.data.users > 1) and (mesh_obj.name != mesh_obj.data.name) # last name check = self is not the "source" of duplinked
                if is_dupLinked:
                    self.report({'INFO'}, f"\"{mesh_obj.name}\": Duplicate-linked object (orig: \"{mesh_obj.data.name}\")")
                    should_bypass = True

            if should_bypass:
                # Store the reference to re-add the modifier later
                linked_meshes.append(mesh_obj)
                # Physically remove the Armature modifier to completely prevent binding distortion
                self.report({'INFO'}, f"Caching & removing Armature modifier from linked mesh: \"{mesh_obj.name}\"")
                for mod in list(mesh_obj.modifiers):
                    if mod.type == 'ARMATURE' and mod.object == obj:
                        mesh_obj.modifiers.remove(mod)
                continue
            


            # --- Previous logic kept intact ONLY for non-linked meshes ---
            has_mirror = any(mod.type == 'MIRROR' and mod.use_clip for mod in mesh_obj.modifiers)
            
            center_mask = []
            for v in mesh_obj.data.vertices:
                if has_mirror and abs(v.co.x) < 0.001:
                    center_mask.append(True)
                else:
                    center_mask.append(False)
            center_vertex_masks[mesh_obj.name] = center_mask

            new_copy = mesh_obj.copy()
            new_copy.data = mesh_obj.data.copy()
            context.scene.collection.objects.link(new_copy)
            mesh_pairings[mesh_obj] = new_copy
            
            context.view_layer.objects.active = new_copy
            for mod in new_copy.modifiers:
                if mod.type == 'ARMATURE' and mod.object == obj:
                    bpy.ops.object.modifier_apply(modifier=mod.name)
                    break

        # --- STEP 5: Apply Rest Pose Globally ---
        context.view_layer.objects.active = obj
        self.report({'INFO'}, "Changing mode to Pose mode to apply updated bone coordinates to Rest Pose.")
        bpy.ops.object.mode_set(mode='POSE')

        apply_globally = context.scene.sync_apply_global_rest
        if apply_globally:
            self.report({'INFO'}, "Appling to Rest Pose: For all bones")
            bpy.ops.pose.armature_apply(selected=False)
        else:
            self.report({'INFO'}, "Appling to Rest Pose: For selected bones only")
            bpy.ops.pose.armature_apply(selected=True)

        context.view_layer.update()

        # --- STEP 6: Restore Geometry Coordinates with Mirror Center Protection ---
        self.report({'INFO'}, "Changing mode to Object mode to restore mesh center vertices.")
        bpy.ops.object.mode_set(mode='OBJECT')

        # 1. Restore non-linked mesh shapes via duplicate data pairing
        for orig_mesh, backup_mesh in mesh_pairings.items():
            center_mask = center_vertex_masks.get(orig_mesh.name, [])
            
            for idx, vertex in enumerate(orig_mesh.data.vertices):
                target_co = backup_mesh.data.vertices[idx].co.copy()
                if idx < len(center_mask) and center_mask[idx]:
                    target_co.x = 0.0
                vertex.co = target_co
            
            orig_mesh.data.update()
            bpy.data.objects.remove(backup_mesh, do_unlink=True)

        # 2. Re-create and bind a fresh Armature modifier for linked objects
        for mesh_obj in linked_meshes:
            self.report({'INFO'}, f"Re-adding Armature modifier to linked mesh: \"{mesh_obj.name}\"")
            # Create a clean, un-cached Armature modifier on the linked mesh
            new_mod = mesh_obj.modifiers.new(name="Armature", type='ARMATURE')
            new_mod.object = obj
            
            # Optional: Move the modifier to the top of the stack if needed
            # bpy.ops.object.modifier_move_to_index(modifier=new_mod.name, index=0)


        # --- STEP 7: Process Animation Actions Across Detected Tracks ---
        context.view_layer.objects.active = obj
        self.report({'INFO'}, "Changing mode to Pose mode to update animation Actions.")
        bpy.ops.object.mode_set(mode='POSE')

        for action in bpy.data.actions:
            keyframes_dict = {}
            curves_map = {}
            
            for b_name, tdata in bone_transforms.items():
                curves_map[b_name] = {'quat': [None]*4, 'loc': [None]*3}
                
                for fcurve in action.fcurves:
                    if fcurve.data_path == tdata['rot_path']:
                        idx = fcurve.array_index
                        if 0 <= idx <= 3:
                            curves_map[b_name]['quat'][idx] = fcurve
                            for kp in fcurve.keyframe_points:
                                keyframes_dict[kp.co.x] = True
                    elif fcurve.data_path == tdata['loc_path']:
                        idx = fcurve.array_index
                        if 0 <= idx <= 2:
                            curves_map[b_name]['loc'][idx] = fcurve
                            for kp in fcurve.keyframe_points:
                                keyframes_dict[kp.co.x] = True

            if not keyframes_dict:
                continue
                
            frame_list = sorted(list(keyframes_dict.keys()))
            
            for frame in frame_list:
                for b_name, tdata in bone_transforms.items():
                    b_curves = curves_map[b_name]
                    q_curves = b_curves['quat']
                    l_curves = b_curves['loc']
                    
                    if any(q_curves) or any(l_curves):
                        # Evaluate rotation channels
                        w = q_curves[0].evaluate(frame) if q_curves[0] else 1.0
                        x = q_curves[1].evaluate(frame) if q_curves[1] else 0.0
                        y = q_curves[2].evaluate(frame) if q_curves[2] else 0.0
                        z = q_curves[3].evaluate(frame) if q_curves[3] else 0.0
                        
                        existing_quat = Quaternion((w, x, y, z))
                        new_quat = tdata['inverse_quat'] @ existing_quat
                        new_quat.normalize()
                        
                        for i in range(4):
                            if q_curves[i]:
                                for kp in q_curves[i].keyframe_points:
                                    if abs(kp.co.x - frame) < 0.01:
                                        kp.co.y = new_quat[i]
                                        q_curves[i].update()
                        
                        # Evaluate location channels
                        lx = l_curves[0].evaluate(frame) if l_curves[0] else 0.0
                        ly = l_curves[1].evaluate(frame) if l_curves[1] else 0.0
                        lz = l_curves[2].evaluate(frame) if l_curves[2] else 0.0
                        
                        existing_loc = Vector((lx, ly, lz))
                        new_loc = tdata['inverse_quat'] @ (existing_loc - tdata['current_loc'])
                        
                        for i in range(3):
                            if l_curves[i]:
                                for kp in l_curves[i].keyframe_points:
                                    if abs(kp.co.x - frame) < 0.01:
                                        kp.co.y = new_loc[i]
                                        l_curves[i].update()

        # Revert back cleanly to the mode the user started in
        if context.mode != original_mode:
            self.report({'INFO'}, f"Changing mode back to original mode ( {original_mode} ).")
            bpy.ops.object.mode_set(mode=original_mode)

        self.report({'INFO'}, "Finished: Rest pose updated, and all Actions modified.")
        return {'FINISHED'}


# --- STEP 8: Create the Sidebar Panel Interface with Checkbox ---
class VIEW3D_PT_auto_rest_pose_panel(bpy.types.Panel):
    """Creates a stable custom UI tab inside the Sidebar (N-panel)"""
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Rest Pose Updater'
    bl_label = "Rest Pose Updater (Preserve Mesh Weights & Actions!!)"

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        
        layout.label(text="This tool does Apply Pose as Rest Pose,", icon='AUTO')
        layout.label(text="while preserving anim Actions and mesh weights.", icon='AUTO')
        layout.separator()

        # New Rest Bake Mode Controls
        layout.label(text="Bones to Apply Pose as Rest Pose:")
        layout.prop(scene, "sync_apply_global_rest", text="Apply for all bones")
        layout.separator()
        layout.label(text="For symmetrical bones,", icon='INFO')
        layout.label(text="if both bones of a symmetrical bone pair have non-symmetrical transforms,")
        layout.label(text="it will choose the bone that has larger transform from the original Rest Pose")
        layout.label(text="to apply symmetrical transform.")
        layout.separator()
        
        # Displays the persistent property toggle natively as a checkbox element
        layout.label(text="Linked mesh handling options:")
        layout.prop(scene, "sync_handle_linked_meshes", text="Bypass Cross-Scene Linked Meshes")
        layout.prop(scene, "sync_handle_duplicate_linked", text="Bypass \"Duplicate Linked (Alt + D)\" Meshes ")
        layout.separator()
        
        layout.operator(POSE_OT_auto_rest_pose_sync.bl_idname, text="Apply Pose as Rest Pose (Preserve!!)")



# Register properties to Scene type so the script can evaluate their states
def register():
    bpy.types.Scene.sync_apply_global_rest = bpy.props.BoolProperty(
        name="Apply For All Bones", # "Global Rest Pose Bake"
        description="True: Apply pose as Rest pose for all bones in Armature.\n\nFalse: Apply pose as Rest pose for selected bones only.",
        default=True
    )

    bpy.types.Scene.sync_handle_linked_meshes = bpy.props.BoolProperty(
        name="Bypass Linked Meshes",
        description="Removes/re-applies Armature modifiers from Linked objects either from another Scene or another .blend file,\n\nset to True if this script causes distortion on weighted linked mesh", # "Physically removes armature modifiers from cross-scene instanced meshes to prevent spatial compression or layout distortion during global bakes"
        default=True
    )
    
    bpy.types.Scene.sync_handle_duplicate_linked = bpy.props.BoolProperty(
        name="Bypass Duplicate Linked",
        description="Removes/re-applies Armature modifiers from \"Duplicate Linked\" objects,\n\nset to True if this script causes distortion on weighted linked mesh", # "Physically removes armature modifiers from objects sharing mesh data containers locally to prevent skinning matrix compression issues"
        default=True
    )
    
    bpy.utils.register_class(POSE_OT_auto_rest_pose_sync)
    bpy.utils.register_class(VIEW3D_PT_auto_rest_pose_panel)


def unregister():
    bpy.utils.unregister_class(POSE_OT_auto_rest_pose_sync)
    bpy.utils.unregister_class(VIEW3D_PT_auto_rest_pose_panel)
    
    # Clean up scene type reference channels during execution unloads
    del bpy.types.Scene.sync_apply_global_rest
    del bpy.types.Scene.sync_handle_linked_meshes
    del bpy.types.Scene.sync_handle_duplicate_linked


if __name__ == "__main__":
    register()
