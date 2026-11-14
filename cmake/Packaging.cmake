# Install layout and the release ZIP.
#
# There is exactly one packaging path: CPack. It writes the archive straight
# into dist/ (the built-in `package` target), and the dist_checksums target
# writes dist/SHA256SUMS.txt for that ZIP. tools/package_rc.ps1 used to be a
# second, partially overlapping path and is gone.

# --------------------------------------------------------------------------- #
# install layout and ZIP (W4.1)
# --------------------------------------------------------------------------- #
#
# The payload is per-user and self-contained: the two executables, the model
# fetcher and its lock, the runtime DLLs, the two config documents and the
# licences. No model weights are installed; models/ is filled by watashi_fetch
# into the user directory.
if(WIN32)
    # Collect the MSVC/UCRT redistributables but add their install rule below,
    # so they land beside the executables instead of in a separate bin/.
    set(CMAKE_INSTALL_SYSTEM_RUNTIME_LIBS_SKIP TRUE)
    include(InstallRequiredSystemLibraries)

    # CTranslate2 links OpenMP, so its DLL imports VCOMP140.DLL. That file is
    # part of the VC++ redistributable but not in CMAKE_INSTALL_SYSTEM_RUNTIME_LIBS,
    # and a clean Windows VM does not have it. Ship it beside ctranslate2.dll.
    set(WATASHI_VCOMP_ROOT "$ENV{ProgramFiles\(x86\)}")
    string(REPLACE "\\" "/" WATASHI_VCOMP_ROOT "${WATASHI_VCOMP_ROOT}")
    file(GLOB WATASHI_VCOMP_CANDIDATES
        "${WATASHI_VCOMP_ROOT}/Microsoft Visual Studio/2022/*/VC/Redist/MSVC/*/x64/Microsoft.VC143.OpenMP/vcomp140.dll")
    if(NOT WATASHI_VCOMP_CANDIDATES)
        file(GLOB WATASHI_VCOMP_CANDIDATES
            "$ENV{VCToolsRedistDir}/x64/Microsoft.VC143.OpenMP/vcomp140.dll")
    endif()
    if(NOT WATASHI_VCOMP_CANDIDATES)
        message(FATAL_ERROR "vcomp140.dll not found; install the MSVC OpenMP redistributable")
    endif()
    list(GET WATASHI_VCOMP_CANDIDATES 0 WATASHI_VCOMP_DLL)
    string(REPLACE "\\" "/" WATASHI_VCOMP_DLL "${WATASHI_VCOMP_DLL}")

    # Only the watashi component belongs in the package. The FetchContent
    # dependencies add their own install rules (sentencepiece headers); without
    # a component, `cmake --install` and CPack would drag those in too.
    install(TARGETS watashi_ui watashi_cli watashi_fetch
        RUNTIME DESTINATION . COMPONENT watashi)
    install(FILES
        "${WATASHI_ORT_DLL}"
        "${WATASHI_DNNL_DLL}"
        "${WATASHI_VCOMP_DLL}"
        "$<TARGET_FILE:ctranslate2>"
        LICENSE
        THIRD_PARTY_LICENSES.md
        models.lock.json
        DESTINATION . COMPONENT watashi)
    install(FILES config/presentation.json config/settings_ui.json
        DESTINATION config COMPONENT watashi)
    install(FILES config/install_manifest.json
        DESTINATION . COMPONENT watashi RENAME install_manifest.json)
    # Compliance payload: the project licence, third-party notices, model
    # licence notes and the generated SPDX SBOM. Model weights are never here.
    install(FILES
        licenses/LICENSE
        licenses/THIRD_PARTY_LICENSES.md
        licenses/MODEL_LICENSES.md
        licenses/sbom.spdx.json
        DESTINATION licenses COMPONENT watashi)
    install(FILES installer/install.ps1 installer/uninstall.ps1
        DESTINATION . COMPONENT watashi)
    if(CMAKE_INSTALL_SYSTEM_RUNTIME_LIBS)
        install(FILES ${CMAKE_INSTALL_SYSTEM_RUNTIME_LIBS}
            DESTINATION . COMPONENT watashi)
    endif()

    set(CPACK_PACKAGE_NAME "ProjectWatashi")
    set(CPACK_PACKAGE_VENDOR "Shir0Tak1na")
    set(CPACK_PACKAGE_VERSION "${WATASHI_VERSION}")
    set(CPACK_PACKAGE_FILE_NAME "ProjectWatashi-${WATASHI_VERSION}-win64")
    set(CPACK_GENERATOR "ZIP")
    set(CPACK_INCLUDE_TOPLEVEL_DIRECTORY ON)
    set(CPACK_COMPONENTS_ALL watashi)
    # Component archives would append "-watashi" to the file name; this keeps
    # the documented name while still packaging only the watashi component
    # (the FetchContent install rules are left out).
    set(CPACK_ARCHIVE_COMPONENT_INSTALL ON)
    set(CPACK_ARCHIVE_WATASHI_FILE_NAME
        "ProjectWatashi-${WATASHI_VERSION}-win64")
    set(CPACK_COMPONENT_WATASHI_DISPLAY_NAME "Project Watashi")
    set(CPACK_COMPONENT_WATASHI_DESCRIPTION
        "Per-user subtitle overlay, engine, model fetcher, config and licences")

    # Packaging writes into the build tree. dist/ at the top of the repository
    # holds the published archive for the released tag and is updated only by
    # the release step (docs/RELEASE.md), so a routine build -- even of a tree
    # that has moved past the tag -- cannot silently replace it.
    set(CPACK_OUTPUT_FILE_PREFIX "${CMAKE_BINARY_DIR}/dist")

    include(CPack)

    # `cmake --build . --config Release --target dist_checksums` runs CPack and
    # then writes dist/SHA256SUMS.txt with the "<hash>  <name>" form sha256sum
    # -c expects. Calling cpack here (rather than depending on the generator's
    # PACKAGE project, which is not a CMake target) keeps the checksum step
    # attached to the same one packaging path: CPack.
    add_custom_target(dist_checksums
        COMMAND ${CMAKE_CPACK_COMMAND} -G ZIP -C $<CONFIG>
            --config ${CMAKE_BINARY_DIR}/CPackConfig.cmake
        COMMAND ${CMAKE_COMMAND}
            -DDIST_DIR=${CMAKE_BINARY_DIR}/dist
            -DZIP_NAME=ProjectWatashi-${WATASHI_VERSION}-win64.zip
            -P ${CMAKE_SOURCE_DIR}/cmake/WriteChecksums.cmake
        COMMENT "Packaging with CPack and writing SHA256SUMS.txt")
endif()
